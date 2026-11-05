import asyncio
import dataclasses
import io
import json
import os
import tarfile
import time
from pathlib import Path

import httpx
import pytest
from test_activation_live import ROOT, _run, _tampered, opa_command

from mint_litellm.plugin.policy.bundle import (
    BundleContentMismatch,
    activate_bundle,
    bundle_content_sha256,
    canonical_json,
    keep_active_bundle,
    loaded_bundle_sha256,
    parse_data_json,
    prepare_bundle,
    publish_bundle,
    register_bundle,
    restore_bundle,
    retry_activation,
)

PATH = Path(os.environ.get("MINT_TEST_OPA_BUNDLE_DIR", ".")) / "mint.tar.gz"
NUMBERS = (
    b'"numbers": [1.0, 1e2, 1.50, -0.0, 12345678901234567890123, '
    b"0.1000000000000000055511151231257827, 1E+30],"
)
NO_COPY = "not restored: no verified copy of the active bundle"


def _opa_url() -> str:
    return os.environ["MINT_TEST_OPA_URL"].rstrip("/")


def _bundles(*names):
    return [
        prepare_bundle(ROOT, name, opa=opa_command(), opa_policy_path="/work/policy")
        for name in names
    ]


def _mismatching(bundle):
    """메타는 그대로 두고 내용만 바꾼 번들(활성화가 내용 불일치로 거절해야 한다)."""
    return dataclasses.replace(bundle, archive=_tampered(
        bundle.archive, "mint/data/data.json", lambda text: text.replace(b"09:00", b"00:00")
    ))


async def _publish(data):
    publish_bundle(PATH, data)


async def _keep(version_id, sha):
    keep_active_bundle(PATH, version_id, sha)


async def _restore(version_id, sha):
    return restore_bundle(PATH, version_id, sha)


SAFE = {"keep": _keep, "restore": _restore}


async def _served(opa_url):
    async with httpx.AsyncClient(trust_env=False) as client:
        meta = (await client.get(f"{opa_url}/v1/data/mint/meta")).json()["result"]
        decision = (await client.post(f"{opa_url}/v1/data/mint/decision", json={"input": {}}))
    return meta, await loaded_bundle_sha256(opa_url), decision.json()["result"]


async def _expect_serving(bundle):
    """OPA의 메타·재계산 내용 해시·호출 판정이 모두 ``bundle``이다."""
    for _ in range(100):
        try:
            meta, content, decision = await _served(_opa_url())
            if meta["policy_version_id"] == bundle.policy_version_id:
                break
        except BundleContentMismatch:
            pass
        await asyncio.sleep(0.1)
    assert meta == {
        "policy_version_id": bundle.policy_version_id, "bundle_sha256": bundle.bundle_sha256
    }
    assert content == bundle.bundle_sha256
    assert (decision["policy_version_id"], decision["bundle_sha256"]) == (
        bundle.policy_version_id, bundle.bundle_sha256
    )


async def _statuses(conn):
    await conn.rollback()
    rows = await (await conn.execute(
        "SELECT id, status FROM mint.policy_versions ORDER BY id"
    )).fetchall()
    await conn.commit()
    return rows


async def _activate(conn, bundle, publish=_publish, seconds=30, **extra):
    await activate_bundle(
        conn, instance_id="test", actor="operator", bundle=bundle,
        publish=publish, opa_url=_opa_url(), timeout_seconds=seconds, **extra,
    )


async def _with_active(conn, good, bad):
    await register_bundle(conn, instance_id="test", bundle=good)
    await _activate(conn, good, **SAFE)
    await register_bundle(conn, instance_id="test", bundle=bad)
    await conn.commit()


@pytest.mark.parametrize("failure", ["content-mismatch", "never-loaded"])
def test_failed_activation_restores_the_previous_bundle(live_database, failure):
    import psycopg

    good, other = _bundles("good", "other")
    bad = _mismatching(other)

    async def stale(_data):
        await _publish(good.archive)

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await _with_active(conn, good, bad)
            expected, publish = (
                (BundleContentMismatch, _publish) if failure == "content-mismatch"
                else (TimeoutError, stale)
            )
            with pytest.raises(expected) as raised:
                await _activate(conn, bad, publish, 10, **SAFE)
            assert raised.value.restore_outcome == "restored"
            assert "previous bundle: restored" in raised.value.__notes__
            assert await _statuses(conn) == [("good", "active"), ("other", "validated")]
            await _expect_serving(good)
            assert PATH.read_bytes() == good.archive

    _run(run)


def test_failed_first_activation_blocks_calls_and_recovers_on_retry(live_database):
    import psycopg

    (other,) = _bundles("other")
    bad = _mismatching(other)
    called = []

    async def restore(version_id, sha):
        called.append(sha)
        return restore_bundle(PATH, version_id, sha)

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await register_bundle(conn, instance_id="test", bundle=bad)
            await conn.commit()
            with pytest.raises(BundleContentMismatch) as raised:
                await _activate(conn, bad, seconds=8, keep=_keep, restore=restore)
            assert raised.value.restore_outcome == "not restored: no active version"
            assert not called
            assert await _statuses(conn) == [("other", "validated")]
            # 활성 판본이 없고, OPA가 서빙하는 번들 내용은 등록된 해시와 다르다 -> 호출 차단 조건
            meta, content, _ = await _served(_opa_url())
            assert meta["policy_version_id"] == "other"
            assert content != other.bundle_sha256
            await retry_activation(
                conn, instance_id="test", actor="operator", bundle=other,
                publish=_publish, opa_url=_opa_url(), timeout_seconds=30, **SAFE,
            )
            assert await _statuses(conn) == [("other", "active")]
            await _expect_serving(other)

    _run(run)


def test_restore_failures_are_reported_in_the_error(live_database):
    import psycopg

    good, other = _bundles("good", "other")
    bad = _mismatching(other)

    async def no_copy(*_):
        return False

    async def broken(*_):
        raise OSError("disk full")

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await _with_active(conn, good, bad)
            for restore, outcome in (
                (no_copy, NO_COPY),
                (broken, "restore failed: OSError: disk full"),
            ):
                with pytest.raises(BundleContentMismatch) as raised:
                    await _activate(conn, bad, seconds=6, keep=_keep, restore=restore)
                assert raised.value.restore_outcome == outcome
                await conn.rollback()
            await _publish(good.archive)
            await _expect_serving(good)

    _run(run)


def test_failed_restore_then_retry_keeps_the_active_copy(live_database):
    """A 활성 -> B 게시 실패 -> 복원 실패 -> B 재시도해도 보관본 A가 B로 덮이지 않는다."""
    import psycopg

    good, other = _bundles("good", "other")
    bad = _mismatching(other)

    async def broken(*_):
        raise OSError("disk full")

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await _with_active(conn, good, bad)
            with pytest.raises(BundleContentMismatch):
                await _activate(conn, bad, seconds=12, keep=_keep, restore=broken)
            await conn.rollback()
            assert PATH.read_bytes() == bad.archive
            with pytest.raises(BundleContentMismatch) as raised:
                await _activate(conn, bad, seconds=12, **SAFE)
            assert raised.value.restore_outcome == "restored"
            assert PATH.read_bytes() == good.archive
            await _expect_serving(good)

    _run(run)


def test_keep_failure_before_publishing_restores_nothing(live_database):
    """보관 단계 오류는 게시 전이므로, 오래된 보관본이 현재 파일을 덮지 않는다."""
    import psycopg

    good, other, stale = _bundles("good", "other", "stale")
    restored = []

    async def broken_keep(*_):
        raise OSError("keep failed")

    async def restore(version_id, sha):
        restored.append(sha)
        return restore_bundle(PATH, version_id, sha)

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await _with_active(conn, good, other)
            PATH.with_name(PATH.name + ".previous").write_bytes(stale.archive)
            with pytest.raises(OSError) as raised:
                await _activate(conn, other, keep=broken_keep, restore=restore)
            assert not hasattr(raised.value, "restore_outcome")
            assert not restored
            assert PATH.read_bytes() == good.archive
            assert await _statuses(conn) == [("good", "active"), ("other", "validated")]
            # 보관본이 오래된 다른 판본이면 복원 대상으로 쓰지 않는다
            with pytest.raises(BundleContentMismatch) as raised:
                await _activate(conn, _mismatching(other), seconds=12, restore=restore)
            assert raised.value.restore_outcome == NO_COPY

    _run(run)


def test_cancellation_after_publishing_restores_before_propagating(live_database):
    import psycopg

    good, other = _bundles("good", "other")

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await _with_active(conn, good, other)
            published = asyncio.Event()

            async def publish_then_hang(data):
                await _publish(data)
                published.set()
                await asyncio.sleep(60)

            task = asyncio.create_task(
                _activate(conn, other, publish_then_hang, 30, **SAFE)
            )
            await published.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as raised:
                await task
            assert raised.value.restore_outcome == "restored"
            assert await _statuses(conn) == [("good", "active"), ("other", "validated")]
            await _expect_serving(good)

    _run(run)


def test_time_spent_waiting_for_the_lock_counts_against_the_total_limit(live_database):
    import psycopg

    good, other = _bundles("good", "other")
    bad = _mismatching(other)

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn, \
                await psycopg.AsyncConnection.connect(live_database) as holder:
            await _with_active(conn, good, bad)

            async def hold():
                async with holder.transaction():
                    await holder.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("test",)
                    )
                    await asyncio.sleep(3)

            blocker = asyncio.create_task(hold())
            await asyncio.sleep(0.5)
            started = time.monotonic()
            with pytest.raises(BundleContentMismatch) as raised:
                await _activate(conn, bad, seconds=10, **SAFE)
            elapsed = time.monotonic() - started
            await blocker
            assert raised.value.restore_outcome == "restored"
            assert elapsed < 10.3
            await _expect_serving(good)

    _run(run)


def test_total_limit_expiring_during_the_restore_still_reports_it(live_database):
    import psycopg

    good, other = _bundles("good", "other")
    bad = _mismatching(other)

    async def slow_restore(version_id, sha):
        restore_bundle(PATH, version_id, sha)
        await asyncio.sleep(60)

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await _with_active(conn, good, bad)
            with pytest.raises(TimeoutError) as raised:
                async with asyncio.timeout(5):
                    await _activate(conn, bad, seconds=6, keep=_keep, restore=slow_restore)
            assert raised.value.__cause__.restore_outcome == "restore interrupted"
            assert PATH.read_bytes() == good.archive

    _run(run)


def test_rejected_commit_restores_the_previous_bundle(live_database):
    import psycopg

    good, other = _bundles("good", "other")

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await _with_active(conn, good, other)
            await conn.execute(
                "CREATE FUNCTION mint_reject() RETURNS trigger LANGUAGE plpgsql AS "
                "$$ BEGIN RAISE EXCEPTION 'commit rejected'; END $$"
            )
            await conn.execute(
                "CREATE CONSTRAINT TRIGGER mint_reject AFTER INSERT ON mint.events "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION mint_reject()"
            )
            await conn.commit()
            with pytest.raises(psycopg.Error) as raised:
                await _activate(conn, other, **SAFE)
            assert raised.value.restore_outcome == "restored"
            assert await _statuses(conn) == [("good", "active"), ("other", "validated")]
            await _expect_serving(good)

    _run(run)


def test_unknown_commit_result_does_not_override_a_successful_activation(live_database):
    import psycopg

    good, other = _bundles("good", "other")

    class LostAck:
        """첫 트랜잭션은 커밋된 뒤 응답 유실로 오류를 낸다."""

        def __init__(self, conn):
            self._conn = conn
            self._first = True

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def transaction(self):
            inner = self._conn.transaction()
            lose = self._first
            self._first = False

            class Wrapper:
                async def __aenter__(_self):
                    return await inner.__aenter__()

                async def __aexit__(_self, *exc):
                    result = await inner.__aexit__(*exc)
                    if lose and exc[0] is None:
                        raise psycopg.OperationalError("connection lost after commit")
                    return result

            return Wrapper()

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await _with_active(conn, good, other)
            with pytest.raises(psycopg.OperationalError) as raised:
                await _activate(LostAck(conn), other, **SAFE)
            assert raised.value.restore_outcome == "not restored: activation was committed"
            assert await _statuses(conn) == [("good", "superseded"), ("other", "active")]
            await _expect_serving(other)

    _run(run)


def test_dead_connection_after_failure_is_not_restored(live_database):
    import psycopg

    good, other = _bundles("good", "other")
    bad = _mismatching(other)

    async def kill_connection(data):
        await _publish(data)
        await conn_holder[0].close()

    conn_holder = []

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as setup:
            await _with_active(setup, good, bad)
        conn = await psycopg.AsyncConnection.connect(live_database)
        conn_holder.append(conn)
        with pytest.raises(Exception) as raised:
            await _activate(conn, bad, kill_connection, 6, **SAFE)
        assert raised.value.restore_outcome == (
            "not restored: connection lost, commit result unknown"
        )

    _run(run)


def test_number_notation_round_trips_through_opa(live_database):
    import psycopg

    (base,) = _bundles("numbers")
    with tarfile.open(fileobj=io.BytesIO(base.archive), mode="r:gz") as reader:
        members = {m.name: reader.extractfile(m).read() for m in reader.getmembers()}
    data_raw = members["mint/data/data.json"].replace(b"{", b"{" + NUMBERS, 1)
    modules = {k: v for k, v in members.items() if k.endswith(".rego")}
    sha = bundle_content_sha256(modules, {"mint/data/data.json": parse_data_json(data_raw)})
    members["mint/data/data.json"] = data_raw
    members["mint/meta/data.json"] = json.dumps(
        {"policy_version_id": "numbers", "bundle_sha256": sha}
    ).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as writer:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 0
            writer.addfile(info, io.BytesIO(content))
    bundle = dataclasses.replace(base, bundle_sha256=sha, archive=output.getvalue())

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await register_bundle(conn, instance_id="test", bundle=bundle)
            await _activate(conn, bundle)
            async with httpx.AsyncClient(trust_env=False) as client:
                reply = await client.get(f"{_opa_url()}/v1/data/mint/data")
            served = parse_data_json(reply.text)["result"]
            assert canonical_json(served) == canonical_json(parse_data_json(data_raw))
            assert [str(n) for n in served["numbers"]] == [
                "1.0", "1e2", "1.50", "-0.0", "12345678901234567890123",
                "0.1000000000000000055511151231257827", "1E+30",
            ]
            assert await loaded_bundle_sha256(_opa_url()) == sha

    _run(run)
