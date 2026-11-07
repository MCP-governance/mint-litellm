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


def add_admins(url):
    """게시 직전 확인이 읽는 운영자(서로 다른 사용자의 admin 둘)."""
    with psycopg.connect(url, autocommit=True) as conn:
        for name, user, digit in (("root", "root-user", "1"), ("second", "u2", "2")):
            conn.execute("INSERT INTO mint.operators (instance_id, name, role, token_hash, user_id)"
                         " VALUES (%s, %s, 'admin', repeat(%s, 64), %s) ON CONFLICT DO NOTHING",
                         (INSTANCE, name, digit, user))


def run_worker(world, which="restored", settings=None):
    """작업 실행기를 한 번 돌려 대기 작업이 없을 때까지 처리한다(되돌림 판정 포함)."""
    from mint_litellm.control.worker import RestoreCheck, Worker

    settings = settings or world["settings"]
    login = url_for(world[which], world["clone"] if which == "restored" else
                    conninfo_to_dict(world["live"])["dbname"], "mint_worker")
    worker = Worker(login, INSTANCE, settings, restore_check=RestoreCheck(settings, 0))
    try:
        worker.open()
        while worker.step():
            pass
        return worker.verdict
    finally:
        worker.close()


def verdict_of(url, settings):
    with psycopg.connect(url, autocommit=True) as conn:
        return judge.Judge().evaluate(conn, settings.export_dir, INSTANCE, exports.keys(settings))


def attest_restored(world, reason="복구 확인"):
    with psycopg.connect(world["restored"]) as conn:
        out = restore.attest(make_ctx(conn), world["settings"], reason)
        conn.commit()
    return out


def test_restore_check_then_attest_publishes_through_the_worker_and_releases_the_block(world):
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
    latest = files.latest_path(settings.export_dir, INSTANCE)
    before = latest.read_bytes()
    out = attest_restored(world)
    assert latest.read_bytes() == before  # 복원 명령은 파일을 쓰지 않는다
    assert [r["kind"] for r in out["reapplied"]] == ["org"]
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        assert fetchone(conn, "SELECT status FROM mint.orgs")["status"] == "closed"
        assert fetchone(conn, "SELECT epoch FROM mint.state_epoch")["epoch"] > file_epoch
        events = fetchall(conn, "SELECT source_key, payload FROM mint.events"
                          " WHERE kind = 'restore_attested'")
        assert len(events) == 1 and events[0]["source_key"] == (
            f"restore:{file_epoch}:1:restore_attested")
        assert events[0]["payload"]["reason"] == "복구 확인"
        request = fetchone(conn, "SELECT * FROM mint.restore_requests")
        assert request["status"] == "pending" and request["kind"] == "attest"
    final = run_worker(world)  # 차단 상태에서도 restore_publish는 실행된다
    assert final.state == judge.OK
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        request = fetchone(conn, "SELECT * FROM mint.restore_requests")
        assert request["status"] == "published"
        assert fetchone(conn, "SELECT count(*) AS n FROM mint.exports WHERE status = 'written'"
                        " AND kind = 'checkpoint'")["n"] >= 1
    kept = settings.export_dir / INSTANCE / "restored" / str(request["id"])
    assert (kept / "latest.json").exists() and (kept / "negative").is_dir()


def test_attest_puts_back_a_missing_row_with_the_same_id_and_is_idempotent(world):
    file_epoch = revoke_and_export(world)
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        conn.execute("DELETE FROM mint.orgs WHERE org_id = 'acme'")
    overtake_epoch(world["restored"], file_epoch)
    first = attest_restored(world)
    assert first["reapplied"] == [{"kind": "org", "id": "acme", "was": "missing_row"}]
    second = attest_restored(world, "한 번 더")  # 반복 attest는 아무것도 다시 하지 않는다
    assert second["reapplied"] == []
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        assert fetchone(conn, "SELECT status FROM mint.orgs")["status"] == "closed"
        assert fetchone(conn, "SELECT count(*) AS n FROM mint.restore_requests")["n"] == 2
    assert run_worker(world).state == judge.OK


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
    out = attest_restored(world)
    assert out["reapplied"] == [{"kind": "denial", "id": str(denial), "was": "missing_row"}]
    with psycopg.connect(world["restored"]) as conn:
        row = fetchone(conn, "SELECT * FROM mint.denials")
        assert row["id"] == denial and row["lifted_at"] is None
        assert (row["scope_type"], row["scope_id"], row["reason"]) == ("org", "acme", "유출 의심")


def seed_rows(url):
    """서버 하나와 활성 관계·승인 하나(복구 DB에도 있는 것)."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("INSERT INTO mint.servers (id, instance_id, server_id, status, url_sha256,"
                     " transport, auth_type, registered_by) VALUES (gen_random_uuid(), %s, 's1',"
                     " 'awaiting_approval', repeat('a', 64), 'http', 'none', 'x')", (INSTANCE,))
        conn.execute("UPDATE mint.servers SET status = 'approved'")
        return add_relation_with_approval(conn, "ops")


def add_relation_with_approval(conn, purpose):
    rel = fetchone(conn, "INSERT INTO mint.usage_relations (id, instance_id, org_id, purpose,"
                   " server_id) VALUES (gen_random_uuid(), %s, 'acme', %s, 's1') RETURNING id",
                   (INSTANCE, purpose))["id"]
    appr = fetchone(
        conn, "INSERT INTO mint.approvals (id, instance_id, org_id, relation_id, class, server_id,"
        " tools, scope_type, scope_id, valid_from, valid_until, basis, granted_by, requested_by,"
        " status) VALUES (gen_random_uuid(), %s, 'acme', %s, 'standard', 's1', ARRAY['t'], 'user',"
        " 'u1', now() - interval '1 day', now() + interval '30 days', '{}', 'root', 'x', 'active')"
        " RETURNING id", (INSTANCE, rel))["id"]
    return rel, appr


def test_attest_restores_revocation_history_and_a_terminated_relation_beside_an_active_one(
    fresh_db, login_roles, tmp_path
):
    """철회 이력(사유·시각)과 종료 관계를 되돌리고 다른 활성 승인은 건드리지 않는다."""
    base = conninfo_to_dict(fresh_db)["dbname"]
    with psycopg.connect(fresh_db) as conn:
        ensure(conn, INSTANCE)
        conn.execute("INSERT INTO mint.orgs (instance_id, org_id, name) VALUES (%s, 'acme', 'a')",
                     (INSTANCE,))
        conn.commit()
    seed_rows(fresh_db)  # 서버(복구 DB에도 있다)와 활성 관계·승인 하나
    clone = f"{base}_restored"
    with psycopg.connect(url_for(fresh_db, "postgres"), autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{clone}" TEMPLATE "{base}"')
    settings = Stage2Settings.from_env(
        {"MINT_EXPORT_DIR": str(tmp_path / "ex"), "MINT_EXPORT_HMAC_KEY": KEY})
    world = {"live": fresh_db, "restored": url_for(fresh_db, clone), "settings": settings,
             "clone": clone}
    try:
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            rel, appr = add_relation_with_approval(conn, "audit")  # 복구 시점 뒤에 생김
            conn.execute("UPDATE mint.approvals SET status = 'revoked', revoked_at = now(),"
                         " revoke_reason = '유출 의심' WHERE id = %s", (appr,))
            conn.execute("UPDATE mint.usage_relations SET status = 'terminating' WHERE id = %s",
                         (rel,))
            conn.execute("UPDATE mint.usage_relations SET status = 'terminated' WHERE id = %s",
                         (rel,))
            revoked_at = fetchone(conn, "SELECT revoked_at FROM mint.approvals WHERE id = %s",
                                  (appr,))["revoked_at"]
            exports.write_export(conn, INSTANCE, settings, "negative_state")
            exports.write_export(conn, INSTANCE, settings, "checkpoint")
            file_epoch = fetchone(conn, "SELECT epoch FROM mint.state_epoch")["epoch"]
        overtake_epoch(world["restored"], file_epoch)
        out = attest_restored(world)
        assert {(r["kind"]) for r in out["reapplied"]} == {"relation", "approval"}
        with psycopg.connect(world["restored"], autocommit=True) as conn:
            row = fetchone(conn, "SELECT * FROM mint.approvals WHERE id = %s", (appr,))
            assert row["status"] == "revoked" and row["revoke_reason"] == "유출 의심"
            assert row["revoked_at"] == revoked_at and row["granted_by"] == "root"
            assert fetchone(conn, "SELECT status FROM mint.usage_relations WHERE id = %s",
                            (rel,))["status"] == "terminated"
            others = fetchall(conn, "SELECT status FROM mint.approvals WHERE id <> %s", (appr,))
            assert [r["status"] for r in others] == ["active"]  # 다른 활성 승인은 그대로
            assert fetchone(conn, "SELECT status FROM mint.usage_relations"
                            " WHERE purpose = 'ops'")["status"] == "active"
        assert run_worker(world).state == judge.OK
    finally:
        with psycopg.connect(url_for(fresh_db, "postgres"), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{clone}" WITH (FORCE)')


def test_attest_refuses_and_lists_rows_whose_parents_are_missing(world):
    with psycopg.connect(world["live"], autocommit=True) as conn:
        conn.execute("INSERT INTO mint.servers (id, instance_id, server_id, status, url_sha256,"
                     " transport, auth_type, registered_by) VALUES (gen_random_uuid(), %s, 's9',"
                     " 'awaiting_approval', repeat('a', 64), 'http', 'none', 'x')", (INSTANCE,))
        conn.execute("UPDATE mint.servers SET status = 'approved'")
        rel, appr = add_relation_with_approval_s9(conn)
        conn.execute("UPDATE mint.approvals SET status = 'revoked', revoked_at = now() "
                     "WHERE id = %s", (appr,))
        exports.write_export(conn, INSTANCE, world["settings"], "negative_state")
        exports.write_export(conn, INSTANCE, world["settings"], "checkpoint")
        file_epoch = fetchone(conn, "SELECT epoch FROM mint.state_epoch")["epoch"]
    overtake_epoch(world["restored"], file_epoch)
    with psycopg.connect(world["restored"]) as conn:
        with pytest.raises(ControlError, match=f"approval:{appr}"):
            restore.attest(make_ctx(conn), world["settings"], "x")
    with psycopg.connect(world["restored"], autocommit=True) as conn:  # 아무것도 커밋되지 않았다
        assert fetchall(conn, "SELECT id FROM mint.approvals") == []
        assert fetchall(conn, "SELECT id FROM mint.restore_requests") == []


def add_relation_with_approval_s9(conn):
    rel = fetchone(conn, "INSERT INTO mint.usage_relations (id, instance_id, org_id, purpose,"
                   " server_id) VALUES (gen_random_uuid(), %s, 'acme', 'p', 's9') RETURNING id",
                   (INSTANCE,))["id"]
    appr = fetchone(
        conn, "INSERT INTO mint.approvals (id, instance_id, org_id, relation_id, class, server_id,"
        " tools, scope_type, scope_id, valid_from, valid_until, basis, granted_by, requested_by,"
        " status) VALUES (gen_random_uuid(), %s, 'acme', %s, 'standard', 's9', ARRAY['t'], 'user',"
        " 'u1', now() - interval '1 day', now() + interval '30 days', '{}', 'root', 'x', 'active')"
        " RETURNING id", (INSTANCE, rel))["id"]
    return rel, appr


def test_a_publish_that_dies_midway_keeps_the_block_and_converges_on_rerun(world, monkeypatch):
    from mint_litellm.control import jobs

    file_epoch = revoke_and_export(world)
    overtake_epoch(world["restored"], file_epoch)
    settings = world["settings"]
    attest_restored(world)
    real = exports.write_export
    calls = []

    def dying(*args, **kwargs):
        if kwargs.get("write_latest") is False and args[3] == "negative_state":
            calls.append(1)
        if kwargs.get("write_latest") is False and args[3] == "negative_state" and len(calls) == 1:
            raise OSError("killed")  # 보관은 끝났고 새 파일은 아직 없다
        return real(*args, **kwargs)

    monkeypatch.setattr(exports, "write_export", dying)
    verdict = run_worker(world)
    assert verdict.blocked  # 새 latest.json이 없어 차단이 유지된다
    assert verdict_of(world["restored"], settings).blocked
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        job = fetchone(conn, "SELECT * FROM mint.jobs WHERE kind = 'restore_publish'")
        assert job["status"] == "pending" and job["attempts"] == 1
        conn.execute("UPDATE mint.jobs SET run_after = now() - interval '1 second'")
    assert run_worker(world).state == judge.OK  # 재수렴: 보관은 다시 하지 않는다
    kept = list((settings.export_dir / INSTANCE / "restored").iterdir())
    assert len(kept) == 1 and (kept[0] / "latest.json").exists()
    assert jobs.KINDS["restore_publish"].external_write is False


def test_a_lost_lease_stops_the_publish_and_a_second_run_finishes_it(world, monkeypatch):
    from mint_litellm.control import jobs

    file_epoch = revoke_and_export(world)
    overtake_epoch(world["restored"], file_epoch)
    attest_restored(world)
    real = exports.write_export
    state = {"first": True}

    def losing(*args, **kwargs):
        if state["first"] and kwargs.get("write_latest") is False:
            state["first"] = False
            raise jobs.Cancelled  # 임대를 잃어 외부 쓰기를 멈춘다
        return real(*args, **kwargs)

    monkeypatch.setattr(exports, "write_export", losing)
    assert run_worker(world).state == judge.OK  # 멈춘 작업은 시도로 세지 않고 바로 이어서 끝난다
    assert state["first"] is False
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        job = fetchone(conn, "SELECT * FROM mint.jobs WHERE kind = 'restore_publish'")
        assert job["status"] == "done" and job["attempts"] == 0
        assert fetchone(conn, "SELECT status FROM mint.restore_requests")["status"] == "published"


def test_concurrent_publishers_are_serialized_by_the_instance_lock(world):
    file_epoch = revoke_and_export(world)
    overtake_epoch(world["restored"], file_epoch)
    attest_restored(world)
    settings = world["settings"]
    holder = psycopg.connect(world["restored"], autocommit=True)
    try:
        holder.execute("SELECT pg_advisory_lock(hashtext(%s))",
                       (f"mint.restore_publish:{INSTANCE}",))
        run_worker(world)  # 다른 실행기가 게시 중이면 이 실행기는 물러난다(파일은 그대로)
        assert not (world["settings"].export_dir / INSTANCE / "restored").exists()
        with psycopg.connect(world["restored"], autocommit=True) as conn:
            job = fetchone(conn, "SELECT * FROM mint.jobs WHERE kind = 'restore_publish'")
            assert job["status"] == "pending" and job["attempts"] == 1
            conn.execute("UPDATE mint.jobs SET run_after = now() - interval '1 second'")
    finally:
        holder.close()  # 세션 잠금이 풀린다
    assert run_worker(world).state == judge.OK
    assert verdict_of(world["restored"], settings).state == judge.OK


def test_accept_stale_needs_two_admins_and_a_missing_or_unverified_file(world):
    revoke_and_export(world)
    settings = world["settings"]
    latest = files.latest_path(settings.export_dir, INSTANCE)
    add_admins(world["live"])
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
        out = restore.accept_stale(ctx, settings, "볼륨 복구", operator("second", "u2"))
        conn.commit()
        assert out["cause"] == "missing"
    assert not latest.exists()  # 명령은 파일을 쓰지 않는다
    assert verdict_of(world["live"], settings).cause == "export_missing"
    assert run_worker(world, "live").state == judge.OK  # 차단 중에도 게시 작업은 돈다
    with psycopg.connect(world["live"], autocommit=True) as conn:
        kinds = [r["kind"] for r in fetchall(conn, "SELECT kind FROM mint.events")]
        assert "restore_stale_accepted" in kinds
        assert fetchone(conn, "SELECT status FROM mint.restore_requests")["status"] == "published"


def test_unverifiable_files_are_set_aside_by_the_published_accept_stale(world):
    revoke_and_export(world)
    settings = world["settings"]
    other = Stage2Settings.from_env(
        {"MINT_EXPORT_DIR": str(settings.export_dir), "MINT_EXPORT_HMAC_KEY": "B" * 43})
    assert verdict_of(world["live"], other).cause == "export_unverified"
    add_admins(world["live"])
    with psycopg.connect(world["live"]) as conn:
        out = restore.accept_stale(make_ctx(conn), other, "키 분실", operator("second", "u2"))
        conn.commit()
    assert run_worker(world, "live", other).state == judge.OK
    kept = settings.export_dir / INSTANCE / "restored" / out["request_id"]
    assert (kept / "latest.json").exists()  # 증거는 지우지 않고 옮겨 둔다


def test_the_first_checkpoint_after_release_leaves_the_exports_row(world):
    file_epoch = revoke_and_export(world)
    overtake_epoch(world["restored"], file_epoch)
    attest_restored(world)
    assert run_worker(world).state == judge.OK
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        rows = fetchall(conn, "SELECT kind, status FROM mint.exports WHERE status = 'written'")
    assert {"negative_state", "checkpoint"} <= {r["kind"] for r in rows}


def test_a_noop_update_after_the_rollback_does_not_hide_the_revocation(world):
    """되돌린 뒤 값이 안 바뀐 UPDATE로 state_version만 올려도 상태 열이 달라 막힌다."""
    file_epoch = revoke_and_export(world)
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        for _ in range(3):  # 버전은 파일 값(2)을 넘는다
            conn.execute("UPDATE mint.orgs SET name = name WHERE org_id = 'acme'")
        row = fetchone(conn, "SELECT state, state_version FROM (SELECT status AS state,"
                       " state_version FROM mint.orgs) t")
        assert row["state"] == "active" and row["state_version"] >= 2
    overtake_epoch(world["restored"], file_epoch)
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        j = judge.Judge()
        keys = exports.keys(world["settings"])
        first = j.evaluate(conn, world["settings"].export_dir, INSTANCE, keys)
        second = j.evaluate(conn, world["settings"].export_dir, INSTANCE, keys)
    assert first.state == judge.HOLD and second.blocked
    assert second.detail["missing_items"] == 1


def test_a_hand_made_request_cannot_release_the_block(world):
    """요청 행과 작업 행을 직접 넣어도 게시 직전 확인이 막는다."""
    file_epoch = revoke_and_export(world)
    overtake_epoch(world["restored"], file_epoch)
    settings = world["settings"]
    latest = files.latest_path(settings.export_dir, INSTANCE)
    before = latest.read_bytes()
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        for kind, key in (("attest", "a"), ("stale_accepted", "b")):
            rid = fetchone(conn, "INSERT INTO mint.restore_requests (id, instance_id, kind,"
                           " reason, requested_by, second_approver, file_epoch, target_epoch)"
                           " VALUES (gen_random_uuid(), %s, %s, 'x', 'root', 'second', %s, %s)"
                           " RETURNING id", (INSTANCE, kind, file_epoch, file_epoch + 1))["id"]
            conn.execute("INSERT INTO mint.jobs (id, instance_id, kind, target_ref,"
                         " idempotency_key) VALUES (gen_random_uuid(), %s, 'restore_publish',"
                         " %s, %s)", (INSTANCE, str(rid), f"restore_publish:{key}"))
    run_worker(world)
    assert latest.read_bytes() == before  # 보관도 게시도 일어나지 않았다
    assert not (settings.export_dir / INSTANCE / "restored").exists()
    with psycopg.connect(world["restored"], autocommit=True) as conn:
        assert {r["status"] for r in fetchall(
            conn, "SELECT status FROM mint.restore_requests")} == {"pending"}


def test_attest_that_only_accepts_a_seal_anchor_mismatch_needs_two_admins(world):
    revoke_and_export(world)
    settings = world["settings"]
    path = files.latest_path(settings.export_dir, INSTANCE)
    doc = files.read_verified(path, exports.keys(settings))
    doc.update(to_seal_seq=1, tail_hash="00" * 32)  # 앵커만 어긋난 파일
    files.atomic_write(path, files.sign(doc, settings.export_hmac_key))
    add_admins(world["live"])
    with psycopg.connect(world["live"]) as conn:
        ctx = make_ctx(conn)
        with pytest.raises(ControlError, match="두 번째 승인"):
            restore.attest(ctx, settings, "앵커 인정")
        out = restore.attest(ctx, settings, "앵커 인정", operator("second", "u2"))
        conn.commit()
    assert out["reapplied"] == []
    assert run_worker(world, "live").state == judge.OK
