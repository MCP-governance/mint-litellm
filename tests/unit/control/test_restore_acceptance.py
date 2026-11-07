"""합격 6: 철회 -> 몇 분 전 시점으로 DB 복구 -> 첫 소비로 세대 추월 -> 60초 두 번 안에 차단.
이어서 복원 해제(`restore check|attest|accept-stale`). 실 Postgres.

복구는 철회 전에 복제해 둔 데이터베이스로 접속을 옮기는 것으로 흉내 낸다(내보내기 디렉터리는
그대로다). 철회는 조직 닫기로 대신한다(부정 상태 일곱 종류가 같은 경로를 탄다).
"""

import asyncio
from dataclasses import replace

import pytest
from ctl_fixtures import INSTANCE, LOGIN_PASSWORDS, make_ctx

psycopg = pytest.importorskip("psycopg")

from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

from mint_litellm.control import exports, seal  # noqa: E402
from mint_litellm.control.context import Operator  # noqa: E402
from mint_litellm.control.db.connection import fetchall, fetchone  # noqa: E402
from mint_litellm.control.db.roles import LOGIN_ROLES  # noqa: E402
from mint_litellm.control.errors import ControlError  # noqa: E402
from mint_litellm.control.services import restore  # noqa: E402
from mint_litellm.control.settings_store import ensure  # noqa: E402
from mint_litellm.control.stage2_config import Stage2Settings  # noqa: E402
from mint_litellm.restore import files, judge  # noqa: E402

KEY = "A" * 43


def url_for(base: str, dbname: str, login: str | None = None) -> str:
    params = conninfo_to_dict(base)
    params["dbname"] = dbname
    if login:
        params.update(user=LOGIN_ROLES[login], password=LOGIN_PASSWORDS[login])
    return make_conninfo(**params)


@pytest.fixture
def world(fresh_db, login_roles, tmp_path):
    """철회 전에 복제해 둔 DB(``restored``)와 원래 DB(``fresh_db``), 내보내기 설정."""
    base = conninfo_to_dict(fresh_db)["dbname"]
    with psycopg.connect(fresh_db) as conn:
        ensure(conn, INSTANCE)
        conn.execute("INSERT INTO mint.orgs (instance_id, org_id, name) VALUES (%s, 'acme', 'a')",
                     (INSTANCE,))
        conn.commit()
    clone = f"{base}_restored"
    with psycopg.connect(url_for(fresh_db, "postgres"), autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{clone}" TEMPLATE "{base}"')
    settings = Stage2Settings.from_env(
        {"MINT_EXPORT_DIR": str(tmp_path / "ex"), "MINT_EXPORT_HMAC_KEY": KEY})
    yield {"live": fresh_db, "restored": url_for(fresh_db, clone), "settings": settings,
           "clone": clone}
    with psycopg.connect(url_for(fresh_db, "postgres"), autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{clone}" WITH (FORCE)')


class PluginLogin:
    """감시가 쓰는 ``_run``만 갖춘 접속기(``mint_plugin_app`` 로그인, 풀 없이)."""

    def __init__(self, url):
        self.url = url

    async def _run(self, work):
        async with await psycopg.AsyncConnection.connect(self.url, autocommit=True) as conn:
            return await work(conn)


def revoke_and_export(world):
    """철회(조직 닫기)를 커밋하고, 봉인하고, 부정 상태를 내보낸다."""
    with psycopg.connect(world["live"], autocommit=True) as conn:
        conn.execute("UPDATE mint.orgs SET status = 'closed' WHERE org_id = 'acme'")
        old = seal.SETTLE_SECONDS
        seal.SETTLE_SECONDS = -1
        try:
            seal.seal_once(conn, INSTANCE)
        finally:
            seal.SETTLE_SECONDS = old
        exports.write_export(conn, INSTANCE, world["settings"], "negative_state")
        exports.write_export(conn, INSTANCE, world["settings"], "checkpoint")
        return fetchone(conn, "SELECT epoch FROM mint.state_epoch")["epoch"]


def overtake_epoch(url, target):
    """복구 뒤의 소비·확인·관측이 세대를 올려 파일 세대를 추월한다."""
    with psycopg.connect(url, autocommit=True) as conn:
        while fetchone(conn, "SELECT epoch FROM mint.state_epoch")["epoch"] <= target:
            conn.execute("UPDATE mint.settings SET mode = mode WHERE instance_id = %s", (INSTANCE,))


def test_acceptance_6_a_restore_that_overtakes_the_epoch_is_blocked_within_two_checks(world):
    from mint_litellm.plugin.config import PluginConfig
    from mint_litellm.plugin.restore_watch import RestoreWatch

    file_epoch = revoke_and_export(world)
    # 철회가 사라진 시점(철회 직전)의 DB로 복구한 뒤 첫 소비로 세대가 파일 세대를 추월한다
    overtake_epoch(world["restored"], file_epoch)
    with psycopg.connect(world["restored"]) as conn:
        assert fetchone(conn, "SELECT status FROM mint.orgs")["status"] == "active"
        assert fetchone(conn, "SELECT epoch FROM mint.state_epoch")["epoch"] > file_epoch
    settings = world["settings"]

    async def scenario():
        database = PluginLogin(url_for(world["restored"], world["clone"], "mint_plugin"))
        config = replace(
            PluginConfig.from_env({"MINT_INSTANCE_ID": INSTANCE, "MINT_DATABASE_URL": "x",
                                   "MINT_OPA_URL": "y"}),
            export_dir=settings.export_dir, export_hmac_key=settings.export_hmac_key,
        )
        watch = RestoreWatch(config, database)
        await watch.refresh()  # 첫 판정: 호출은 아직 막지 않는다
        first = (watch.blocked_cause, watch.verdict.state, watch.verdict.detail)
        await watch.refresh()  # 60초 뒤 둘째 판정: 차단
        return first, watch.blocked_cause, watch.verdict.detail

    first, second_cause, detail = asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)
    assert first[0] is None and first[1] == judge.HOLD
    assert second_cause == "restore_pending"
    assert detail["missing_items"] == 1 and detail["db_epoch"] > detail["file_epoch"]


def operator(name="root", user="root-user", role="admin"):
    return Operator(name, role, user)


def test_restore_check_attest_releases_the_block_and_records_the_event(world):
    file_epoch = revoke_and_export(world)
    overtake_epoch(world["restored"], file_epoch)
    settings = world["settings"]
    with psycopg.connect(world["restored"]) as conn:
        report = restore.check(conn, INSTANCE, settings)
        assert report["mismatch"] and [i["kind"] for i in report["items"]] == ["org"]
        assert report["file_epoch"] == file_epoch
        ctx = make_ctx(conn)
        with pytest.raises(ControlError, match="--reason"):
            restore.attest(ctx, settings, " ")
        out = restore.attest(ctx, settings, "복구 확인")
        conn.commit()
    assert out["reapplied"] == [{"kind": "org", "id": "acme"}]
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        assert fetchone(conn, "SELECT status FROM mint.orgs")["status"] == "closed"
        assert fetchone(conn, "SELECT epoch FROM mint.state_epoch")["epoch"] > file_epoch
        events = fetchall(conn, "SELECT source_key, payload FROM mint.events"
                          " WHERE kind = 'restore_attested'")
        assert len(events) == 1 and events[0]["source_key"] == (
            f"restore:{file_epoch}:1:restore_attested")
        assert events[0]["payload"]["reason"] == "복구 확인"
        j = judge.Judge()
        verdict = j.evaluate(conn, settings.export_dir, INSTANCE, exports.keys(settings))
        assert verdict.state == judge.OK  # 새 파일이 DB 기준으로 쓰였다


def test_attest_notes_rows_that_are_absent_in_the_restored_database(world):
    """없음이 곧 차단인 종류는 진행하고 「효력 없음」으로 표시한다."""
    file_epoch = revoke_and_export(world)
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        conn.execute("DELETE FROM mint.orgs WHERE org_id = 'acme'")
    overtake_epoch(world["restored"], file_epoch)
    with psycopg.connect(world["restored"]) as conn:
        out = restore.attest(make_ctx(conn), world["settings"], "x")
        conn.commit()
    assert out["absent"] == [
        {"kind": "org", "id": "acme", "note": "복구 DB에 없음(효력 없음)"}]
    assert out["reapplied"] == []


def test_attest_recreates_a_denial_with_the_same_id_so_the_subject_is_denied_again(world):
    with psycopg.connect(world["live"], autocommit=True) as conn:
        denial = fetchone(conn, "INSERT INTO mint.denials (id, instance_id, org_id, scope_type,"
                          " scope_id, reason, created_by) VALUES (gen_random_uuid(), %s, 'acme',"
                          " 'org', 'acme', '유출 의심', 'root') RETURNING id", (INSTANCE,))["id"]
        exports.write_export(conn, INSTANCE, world["settings"], "negative_state")
        exports.write_export(conn, INSTANCE, world["settings"], "checkpoint")
        file_epoch = fetchone(conn, "SELECT epoch FROM mint.state_epoch")["epoch"]
    overtake_epoch(world["restored"], file_epoch)  # 거부를 만들기 전 시점으로 복구했다
    with psycopg.connect(world["restored"]) as conn:
        assert fetchall(conn, "SELECT id FROM mint.denials") == []
        out = restore.attest(make_ctx(conn), world["settings"], "복구 확인")
        conn.commit()
    assert out["reapplied"] == [{"kind": "denial", "id": str(denial), "recreated": True}]
    with psycopg.connect(world["restored"]) as conn:
        row = fetchone(conn, "SELECT * FROM mint.denials")
        assert row["id"] == denial and row["lifted_at"] is None
        assert (row["scope_type"], row["scope_id"], row["reason"]) == ("org", "acme", "유출 의심")


def test_the_first_checkpoint_after_release_leaves_the_exports_row(world):
    from mint_litellm.control.worker import RestoreCheck, Worker

    file_epoch = revoke_and_export(world)
    overtake_epoch(world["restored"], file_epoch)
    settings = world["settings"]
    with psycopg.connect(world["restored"]) as conn:
        restore.attest(make_ctx(conn), settings, "복구 확인")
        conn.commit()
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        before = fetchone(conn, "SELECT count(*) AS n FROM mint.exports"
                          " WHERE kind = 'checkpoint' AND status = 'written'")["n"]
    login = url_for(world["restored"], world["clone"], "mint_worker")
    worker = Worker(login, INSTANCE, settings, restore_check=RestoreCheck(settings, 0))
    try:
        worker.open()
        worker.enqueue_startup_checkpoint()
        while worker.step():
            pass
        assert worker.verdict.state == judge.OK
    finally:
        worker.close()
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        after = fetchone(conn, "SELECT count(*) AS n FROM mint.exports"
                         " WHERE kind = 'checkpoint' AND status = 'written'")["n"]
    assert after >= before + 1  # 기동 체크포인트(와 일정이 등록한 것)가 행을 남긴다


def test_accept_stale_needs_two_admins_and_a_missing_or_unverified_file(world):
    revoke_and_export(world)
    settings = world["settings"]
    latest = files.latest_path(settings.export_dir, INSTANCE)
    with psycopg.connect(world["live"]) as conn:
        ctx = make_ctx(conn)
        with pytest.raises(ControlError, match="없거나 검증되지"):
            restore.accept_stale(ctx, settings, "r", operator("second", "u2"))
        latest.unlink()  # 볼륨 누락
        with pytest.raises(ControlError, match="두 번째 승인"):
            restore.accept_stale(ctx, settings, "r", None)
        with pytest.raises(ControlError, match="두 번째 승인"):
            restore.accept_stale(ctx, settings, "r", operator("same", "root-user"))
        with pytest.raises(ControlError, match="두 번째 승인"):
            restore.accept_stale(ctx, settings, "r", operator("v", "u9", role="viewer"))
        assert judge.Judge().evaluate(
            conn, settings.export_dir, INSTANCE, exports.keys(settings)).cause == "export_missing"
        out = restore.accept_stale(ctx, settings, "볼륨 복구", operator("second", "u2"))
        conn.commit()
        assert out["cause"] == "missing"
        verdict = judge.Judge().evaluate(
            conn, settings.export_dir, INSTANCE, exports.keys(settings))
        assert verdict.state == judge.OK
        kinds = [r["kind"] for r in fetchall(conn, "SELECT kind FROM mint.events")]
        assert "restore_stale_accepted" in kinds
    # 증거로 남은 옛 부정 상태 파일은 지우지 않고 이름만 바뀐다
    assert list((settings.export_dir / INSTANCE).glob("negative.stale-*"))


def test_unverifiable_files_are_set_aside_by_accept_stale(world, tmp_path):
    revoke_and_export(world)
    settings = world["settings"]
    other = Stage2Settings.from_env(
        {"MINT_EXPORT_DIR": str(settings.export_dir), "MINT_EXPORT_HMAC_KEY": "B" * 43})
    with psycopg.connect(world["live"]) as conn:
        verdict = judge.Judge().evaluate(
            conn, other.export_dir, INSTANCE, exports.keys(other))
        assert verdict.cause == "export_unverified"
        restore.accept_stale(make_ctx(conn), other, "키 분실", operator("second", "u2"))
        conn.commit()
        assert judge.Judge().evaluate(
            conn, other.export_dir, INSTANCE, exports.keys(other)).state == judge.OK

