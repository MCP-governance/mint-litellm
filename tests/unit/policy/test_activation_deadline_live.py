"""활성화의 하나뿐인 마감, 새 연결 복원, 동시 활성화, 짧은 제한의 실 DB·실 OPA 시험."""

import asyncio
import time

import pytest
from test_activation_live import _run
from test_activation_restore_live import (
    NO_COPY,
    PATH,
    SAFE,
    _activate,
    _bundles,
    _expect_serving,
    _keep,
    _mismatching,
    _opa_url,
    _publish,
    _served,
    _statuses,
    _with_active,
    restore_bundle,
)

from mint_litellm.plugin.policy.bundle import register_bundle


def _connector(url):
    import psycopg

    return lambda: psycopg.AsyncConnection.connect(url)


async def _hold_row(url, version_id):
    """다른 연결이 활성 판본 행을 잠가 전환 문장을 막는다. 반환된 함수가 잠금을 푼다."""
    import psycopg

    holder = await psycopg.AsyncConnection.connect(url)
    await holder.execute(
        "SELECT 1 FROM mint.policy_versions WHERE id = %s FOR UPDATE", (version_id,)
    )

    async def release():
        await holder.rollback()
        await holder.close()

    return release


async def _wait_blocked(url):
    import psycopg

    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as probe:
        for _ in range(100):
            row = await (await probe.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND datname = current_database()"
            )).fetchone()
            if row[0]:
                return
            await asyncio.sleep(0.1)
    raise AssertionError("the switch statement never blocked")


def test_short_limit_without_enough_restore_share_stays_fail_closed(live_database):
    import psycopg

    good, other = _bundles("good", "other")
    bad = _mismatching(other)

    async def restore_without_effect(version_id, sha):
        return True  # 파일을 되돌리지 못한 채 성공을 주장해도 OPA 확인이 막는다

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await _with_active(conn, good, bad)
            started = time.monotonic()
            with pytest.raises(Exception) as raised:
                await _activate(
                    conn, bad, seconds=3, keep=_keep, restore=restore_without_effect
                )
            assert time.monotonic() - started < 3 + 1.5
            assert raised.value.restore_outcome.startswith("restore failed: TimeoutError")
            assert await _statuses(conn) == [("good", "active"), ("other", "validated")]
            meta, _, _ = await _served(_opa_url())
            assert meta["policy_version_id"] != "good"  # 서빙은 그대로이고 판본 불일치로 차단된다

    _run(run)


@pytest.mark.parametrize("second_is_bad", [False, True])
def test_two_operators_activating_at_once_end_consistent(live_database, second_is_bad):
    import psycopg

    good, one, two = _bundles("good", "one", "two")
    second = _mismatching(two) if second_is_bad else two

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as a, \
                await psycopg.AsyncConnection.connect(live_database) as b:
            await register_bundle(a, instance_id="test", bundle=good)
            await _activate(a, good, **SAFE)
            await register_bundle(a, instance_id="test", bundle=one)
            await register_bundle(a, instance_id="test", bundle=second)
            await a.commit()
            results = await asyncio.gather(
                _activate(a, one, **SAFE), _activate(b, second, **SAFE),
                return_exceptions=True,
            )
            assert results[0] is None
            rows = dict(await _statuses(a))
            actives = [name for name, status in rows.items() if status == "active"]
            assert len(actives) == 1
            if second_is_bad:
                assert results[1].restore_outcome in {"restored", NO_COPY}
                assert actives == ["one"] or results[1].restore_outcome == "restored"
            else:
                assert results[1] is None
                assert actives == ["two"]
            await _expect_serving({"good": good, "one": one, "two": two}[actives[0]])

    _run(run)


def test_truncated_kept_copy_is_not_restored(live_database):
    import psycopg

    good, other = _bundles("good", "other")
    bad = _mismatching(other)

    async def truncate_then_restore(version_id, sha):
        previous = PATH.with_name(PATH.name + ".previous")
        previous.write_bytes(previous.read_bytes()[:40])
        return restore_bundle(PATH, version_id, sha)

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await _with_active(conn, good, bad)
            with pytest.raises(Exception) as raised:
                await _activate(conn, bad, keep=_keep, restore=truncate_then_restore)
            assert raised.value.restore_outcome == NO_COPY
            assert await _statuses(conn) == [("good", "active"), ("other", "validated")]

    _run(run)


def test_cancelled_mid_statement_restores_through_a_new_connection(live_database):
    import psycopg

    good, other = _bundles("good", "other")

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await _with_active(conn, good, other)
            release = await _hold_row(live_database, "good")
            task = asyncio.create_task(
                _activate(conn, other, **SAFE, connect=_connector(live_database))
            )
            await _wait_blocked(live_database)
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as raised:
                await task
            await release()
            assert raised.value.restore_outcome == "restored"
            async with await psycopg.AsyncConnection.connect(live_database) as check:
                assert await _statuses(check) == [("good", "active"), ("other", "validated")]
            await _expect_serving(good)

    _run(run)


def test_unresponsive_statement_ends_at_the_deadline_and_restores(live_database):
    import psycopg

    good, other = _bundles("good", "other")

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await _with_active(conn, good, other)
            release = await _hold_row(live_database, "good")
            started = time.monotonic()
            with pytest.raises(TimeoutError) as raised:
                await _activate(
                    conn, other, seconds=8, **SAFE, connect=_connector(live_database)
                )
            elapsed = time.monotonic() - started
            await release()
            assert elapsed < 8 + 3
            assert raised.value.restore_outcome == "restored"
            async with await psycopg.AsyncConnection.connect(live_database) as check:
                assert await _statuses(check) == [("good", "active"), ("other", "validated")]
            await _expect_serving(good)

    _run(run)


def test_unresponsive_commit_ends_at_the_deadline_and_restores(live_database):
    import psycopg

    good, other = _bundles("good", "other")

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await _with_active(conn, good, other)
            await conn.execute(
                "CREATE FUNCTION mint_slow() RETURNS trigger LANGUAGE plpgsql AS "
                "$$ BEGIN PERFORM pg_sleep(120); RETURN NULL; END $$"
            )
            await conn.execute(
                "CREATE CONSTRAINT TRIGGER mint_slow AFTER INSERT ON mint.events "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION mint_slow()"
            )
            await conn.commit()
            started = time.monotonic()
            with pytest.raises(TimeoutError) as raised:
                await _activate(
                    conn, other, seconds=8, **SAFE, connect=_connector(live_database)
                )
            assert time.monotonic() - started < 8 + 3
            assert raised.value.restore_outcome == "restored"
            async with await psycopg.AsyncConnection.connect(live_database) as check:
                assert await _statuses(check) == [("good", "active"), ("other", "validated")]
            await _expect_serving(good)

    _run(run)


def test_dead_connection_is_judged_through_a_new_connection(live_database):
    import psycopg

    good, other = _bundles("good", "other")
    bad = _mismatching(other)
    holder = []

    async def kill_connection(data):
        await _publish(data)
        await holder[0].close()

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as setup:
            await _with_active(setup, good, bad)
        conn = await psycopg.AsyncConnection.connect(live_database)
        holder.append(conn)
        with pytest.raises(Exception) as raised:
            await _activate(
                conn, bad, kill_connection, 6, **SAFE, connect=_connector(live_database)
            )
        assert raised.value.restore_outcome == "restored"
        await _expect_serving(good)

    _run(run)
