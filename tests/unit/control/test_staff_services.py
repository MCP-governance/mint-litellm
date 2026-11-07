"""직원 서비스: 실제 직원 로그인(표를 읽지 못함)과 제어 로그인(확인용 읽기)으로 검증한다.
MINT_TEST_DATABASE_URL이 없으면 건너뛴다."""

import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from ctl_fixtures import INSTANCE, as_login

psycopg = pytest.importorskip("psycopg")

from mint_litellm.control.db.connection import fetchall, fetchone  # noqa: E402
from mint_litellm.control.errors import AdminCode, AdminError, ControlError  # noqa: E402
from mint_litellm.control.services import staff  # noqa: E402
from mint_litellm.control.settings_store import ensure  # noqa: E402

HEX = "ab" * 32
USER = "human-1"
OTHER = "human-2"
HARNESS_KEY = "key-harness"
CONSOLE_KEY = "key-console"
SESSION = "11" * 32
SESSION_2 = "22" * 32


def later(**kwargs):
    return datetime.now(UTC) + timedelta(**kwargs)


@pytest.fixture
def world(fresh_db, login_roles):
    """조직·서버·이용 관계·정책 판본이 있는 데이터베이스와 직원·제어 연결."""
    with psycopg.connect(fresh_db) as admin:
        ensure(admin, INSTANCE)
        admin.execute("INSERT INTO mint.orgs (instance_id, org_id, name) VALUES (%s, 'acme', 'A')",
                      (INSTANCE,))
        admin.execute(
            "INSERT INTO mint.servers (id, instance_id, server_id, status, url_sha256, transport,"
            " auth_type, registered_by) VALUES (gen_random_uuid(), %s, 'srv', 'awaiting_approval',"
            " %s, 'http', 'none', 'root')",
            (INSTANCE, HEX),
        )
        admin.execute("UPDATE mint.servers SET status = 'approved' WHERE server_id = 'srv'")
        admin.execute(
            "INSERT INTO mint.usage_relations (id, instance_id, org_id, purpose, server_id)"
            " VALUES (gen_random_uuid(), %s, 'acme', 'ops', 'srv')",
            (INSTANCE,),
        )
        for user in (USER, OTHER):
            admin.execute(
                "INSERT INTO mint.org_bindings (id, instance_id, org_id, subject_type, subject_id,"
                " created_by) VALUES (gen_random_uuid(), %s, 'acme', 'user', %s, 'root')",
                (INSTANCE, user),
            )
        admin.execute(
            "INSERT INTO mint.policy_versions (instance_id, id, catalog_sha256, bundle_sha256)"
            " VALUES (%s, 'v1', %s, %s)",
            (INSTANCE, HEX, HEX),
        )
    with as_login(fresh_db, "mint_staff") as staff_conn, as_login(
        fresh_db, "mint_control"
    ) as control_conn:
        yield fresh_db, staff_conn, control_conn


def sctx(world, user=USER):
    _, staff_conn, control_conn = world
    return staff.StaffContext(staff_conn, control_conn, INSTANCE, user, consent_max_days=90)


def add_confirmation(
    url, user=USER, *, expires=None, requested_key=HARNESS_KEY, tool="echo", display=None,
):
    cid = uuid.uuid4()
    with psycopg.connect(url) as admin:
        admin.execute(
            "INSERT INTO mint.self_confirmations (id, instance_id, org_id, principal_id,"
            " server_id, tool, args_fp, fp_key_version, policy_version_id, purpose,"
            " requested_by_key_token, expires_at, display)"
            " VALUES (%s, %s, 'acme', %s, 'srv', %s, %s, 1, 'v1', '결제', %s, %s, %s::jsonb)",
            (cid, INSTANCE, user, tool, HEX, requested_key, expires or later(minutes=10), display),
        )
    return str(cid)


def row(url, sql, *params):
    with psycopg.connect(url) as admin:
        return fetchone(admin, sql, params)


def login(world, user=USER, session=SESSION, key=CONSOLE_KEY):
    staff.start_session(sctx(world, user), session, key, later(minutes=30), later(hours=8))


# --- 세션 ------------------------------------------------------------------


def test_a_staff_session_starts_with_an_event_and_ends_with_one(world):
    url = world[0]
    login(world)
    kinds = [r["kind"] for r in fetchall(
        psycopg.connect(url), "SELECT kind FROM mint.events ORDER BY inserted_at")]
    assert kinds == ["console_session_started"]
    staff.touch_session(sctx(world), SESSION, later(minutes=45))
    staff.end_session(sctx(world), SESSION)
    assert row(url, "SELECT revoked_at FROM mint.console_sessions")["revoked_at"] is not None
    with pytest.raises(AdminError) as raised:  # 끝난 세션으로는 쓰기를 하지 못한다
        staff.touch_session(sctx(world), SESSION, later(minutes=45))
    assert raised.value.code == AdminCode.FORBIDDEN


def test_session_expiry_inputs_are_validated(world):
    with pytest.raises(ControlError):
        staff.start_session(sctx(world), SESSION, CONSOLE_KEY, later(hours=9), later(hours=8))
    with pytest.raises(ControlError):
        staff.start_session(sctx(world), SESSION, CONSOLE_KEY, later(minutes=-1), later(hours=8))


# --- 본인 확인 -------------------------------------------------------------


def test_a_confirmation_from_another_key_is_confirmed_with_the_session_key(world):
    url = world[0]
    cid = add_confirmation(url)
    login(world)
    staff.confirm(sctx(world), SESSION, cid)
    state = row(url, "SELECT status, confirmed_by_user_id, confirmed_by_key_token"
                     " FROM mint.self_confirmations WHERE id = %s", cid)
    assert state == {"status": "confirmed", "confirmed_by_user_id": USER,
                     "confirmed_by_key_token": CONSOLE_KEY}
    event = row(url, "SELECT kind, actor, payload FROM mint.events WHERE kind ="
                     " 'call_approval_approved'")
    assert event["actor"] == USER and event["payload"]["kind"] == "confirmation"


def test_the_same_key_cannot_confirm_unless_the_setting_allows_it(world):
    url = world[0]
    cid = add_confirmation(url, requested_key=CONSOLE_KEY)
    login(world, key=CONSOLE_KEY)
    with pytest.raises(AdminError) as raised:
        staff.confirm(sctx(world), SESSION, cid)
    assert raised.value.code == AdminCode.SEPARATION_OF_DUTIES
    assert row(url, "SELECT status FROM mint.self_confirmations")["status"] == "pending"
    staff.reject(sctx(world), SESSION, cid)  # 거절은 같은 키로도 할 수 있다
    assert row(url, "SELECT status FROM mint.self_confirmations")["status"] == "rejected"
    with psycopg.connect(url) as admin:
        admin.execute("UPDATE mint.settings SET require_separate_confirm_key = false")
    other = add_confirmation(url, requested_key=CONSOLE_KEY, tool="other")
    staff.confirm(sctx(world), SESSION, other)
    assert row(url, "SELECT status FROM mint.self_confirmations WHERE id = %s", other) == {
        "status": "confirmed"}


def test_an_expired_confirmation_is_stale_evidence(world):
    url = world[0]
    cid = add_confirmation(url, expires=later(seconds=-5))
    login(world)
    with pytest.raises(AdminError) as raised:
        staff.confirm(sctx(world), SESSION, cid)
    assert raised.value.code == AdminCode.STALE_EVIDENCE
    assert row(url, "SELECT status FROM mint.self_confirmations")["status"] == "pending"


def test_only_the_owner_sees_a_confirmation_and_a_second_decision_conflicts(world):
    url = world[0]
    mine = add_confirmation(url)
    login(world)
    login(world, user=OTHER, session=SESSION_2, key="key-other")
    with pytest.raises(AdminError) as raised:
        staff.confirm(sctx(world, OTHER), SESSION_2, mine)
    assert raised.value.code == AdminCode.NOT_FOUND
    staff.confirm(sctx(world), SESSION, mine)
    with pytest.raises(AdminError) as raised:
        staff.confirm(sctx(world), SESSION, mine)
    assert raised.value.code == AdminCode.CONFLICT
    staff.cancel(sctx(world), SESSION, mine)  # 확정된 확인은 소비 전까지 취소할 수 있다
    assert row(url, "SELECT status FROM mint.self_confirmations")["status"] == "cancelled"


def test_an_expired_session_cannot_decide(world):
    url = world[0]
    cid = add_confirmation(url)
    login(world)
    with psycopg.connect(url) as admin:
        admin.execute("UPDATE mint.console_sessions SET idle_expiry = now() - interval '1 second'")
    with pytest.raises(AdminError) as raised:
        staff.confirm(sctx(world), SESSION, cid)
    assert raised.value.code == AdminCode.FORBIDDEN
    assert row(url, "SELECT status FROM mint.self_confirmations")["status"] == "pending"


def test_the_acting_user_does_not_leak_between_calls_on_one_connection(world):
    url = world[0]
    mine = add_confirmation(url)
    login(world)
    assert [r["id"] for r in staff.list_confirmations(sctx(world))] == [uuid.UUID(mine)]
    assert staff.list_confirmations(sctx(world, OTHER)) == []
    assert world[1].execute("SELECT current_setting('mint.staff_user_id', true)").fetchone() in (
        ("",), (None,))


# --- 본인 동의 -------------------------------------------------------------


def consent_args(**overrides):
    values = dict(org_id="acme", server_id="srv", purpose="ops", data_scope="all",
                  text_version="v1", valid_until=later(days=30))
    values.update(overrides)
    return values


def test_a_consent_is_granted_for_the_own_org_and_recorded(world):
    url = world[0]
    login(world)
    consent_id = staff.grant_consent(sctx(world), SESSION, **consent_args())
    state = row(url, "SELECT human_user_id, source, registered_by FROM mint.consents WHERE id = %s",
                consent_id)
    assert state == {"human_user_id": USER, "source": "self_service", "registered_by": USER}
    assert row(url, "SELECT kind FROM mint.events WHERE kind = 'consent_granted'")
    assert [r["id"] for r in staff.list_consents(sctx(world))] == [uuid.UUID(consent_id)]
    staff.revoke_consent(sctx(world), SESSION, consent_id)
    assert row(url, "SELECT revoked_at FROM mint.consents")["revoked_at"] is not None


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        (dict(valid_until=later(days=91)), ControlError),  # 상한
        (dict(valid_until=later(seconds=-1)), ControlError),
        (dict(valid_until=datetime.now() + timedelta(days=1)), ControlError),  # 시간대 없음
        (dict(purpose=" "), ControlError),
        (dict(org_id="no-such-org"), ControlError),
        (dict(server_id="other-server"), AdminError),  # 이 조직의 이용 관계에 없는 서버
    ],
)
def test_consent_inputs_outside_the_rules_are_refused(world, overrides, error):
    login(world)
    with pytest.raises(error):
        staff.grant_consent(sctx(world), SESSION, **consent_args(**overrides))
    assert row(world[0], "SELECT count(*) AS n FROM mint.consents")["n"] == 0


def test_a_user_outside_the_org_and_a_suspended_server_are_refused(world):
    url = world[0]
    with psycopg.connect(url) as admin:
        admin.execute("INSERT INTO mint.orgs (instance_id, org_id, name) VALUES (%s, 'beta', 'B')",
                      (INSTANCE,))
    login(world)
    with pytest.raises(AdminError) as raised:
        staff.grant_consent(sctx(world), SESSION, **consent_args(org_id="beta"))
    assert raised.value.code == AdminCode.FORBIDDEN
    # 팀 바인딩으로 속한 사용자는 팀 목록을 넘기면 인정된다
    with psycopg.connect(url) as admin:
        admin.execute(
            "INSERT INTO mint.org_bindings (id, instance_id, org_id, subject_type, subject_id,"
            " created_by) VALUES (gen_random_uuid(), %s, 'beta', 'team', 't1', 'root')",
            (INSTANCE,),
        )
    with pytest.raises(AdminError):  # 그래도 beta에는 서버 이용 관계가 없다
        staff.grant_consent(sctx(world), SESSION, **consent_args(org_id="beta"), team_ids=["t1"])
    with psycopg.connect(url) as admin:
        admin.execute("UPDATE mint.servers SET status = 'suspended' WHERE server_id = 'srv'")
    with pytest.raises(ControlError, match="승인된 서버가 아니어서"):
        staff.grant_consent(sctx(world), SESSION, **consent_args())


def test_the_consent_ceiling_comes_from_the_context(world):
    login(world)
    _, staff_conn, control_conn = world
    short = staff.StaffContext(staff_conn, control_conn, INSTANCE, USER, consent_max_days=7)
    with pytest.raises(ControlError, match="7일"):
        staff.grant_consent(short, SESSION, **consent_args(valid_until=later(days=30)))
    staff.grant_consent(short, SESSION, **consent_args(valid_until=later(days=6)))


def test_org_membership_is_decided_by_the_first_matching_binding(world):
    url = world[0]
    with psycopg.connect(url) as admin:
        admin.execute("INSERT INTO mint.orgs (instance_id, org_id, name) VALUES (%s, 'beta', 'B')",
                      (INSTANCE,))
        admin.execute(
            "INSERT INTO mint.org_bindings (id, instance_id, org_id, subject_type, subject_id,"
            " created_by) VALUES (gen_random_uuid(), %s, 'beta', 'team', 't1', 'root')",
            (INSTANCE,),
        )
        admin.execute(
            "INSERT INTO mint.usage_relations (id, instance_id, org_id, purpose, server_id)"
            " VALUES (gen_random_uuid(), %s, 'beta', 'ops', 'srv')",
            (INSTANCE,),
        )
    login(world)
    # 사용자 바인딩(acme)이 있으면 팀 바인딩(beta)은 보지 않는다
    with pytest.raises(AdminError) as raised:
        staff.grant_consent(sctx(world), SESSION, **consent_args(org_id="beta"), team_ids=["t1"])
    assert raised.value.code == AdminCode.FORBIDDEN
    # 사용자 바인딩이 없으면 팀 바인딩이 소속을 정한다
    with psycopg.connect(url) as admin:
        admin.execute("DELETE FROM mint.org_bindings WHERE subject_type = 'user'")
    staff.grant_consent(sctx(world), SESSION, **consent_args(org_id="beta"), team_ids=["t1"])
    with pytest.raises(AdminError):
        staff.grant_consent(sctx(world), SESSION, **consent_args(), team_ids=["t1"])


def test_a_staff_transaction_refuses_a_connection_inside_a_transaction(world):
    login(world)
    context = sctx(world)
    with context.staff.transaction():
        with pytest.raises(RuntimeError):
            staff.list_consents(context)


# --- 선확인 뒤 상태가 바뀌어도 직원 쓰기 함수가 다시 막는다 ------------------

@pytest.mark.parametrize("change", ["session", "server", "membership", "relation", "org"])
def test_confirmation_rechecks_state_after_the_service_read(world, monkeypatch, change):
    url = world[0]
    cid = add_confirmation(url)
    login(world)
    original = staff._confirmation

    def read_then_change(*args):
        result = original(*args)
        with psycopg.connect(url) as admin:
            statements = {
                "session": "UPDATE mint.console_sessions SET revoked_at = now()",
                "server": "UPDATE mint.servers SET status = 'suspended'",
                "membership": "DELETE FROM mint.org_bindings WHERE subject_type = 'user'",
                "relation": "UPDATE mint.usage_relations SET status = 'terminating'",
                "org": "UPDATE mint.orgs SET status = 'closed'",
            }
            admin.execute(statements[change])
        return result

    monkeypatch.setattr(staff, "_confirmation", read_then_change)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        staff.confirm(sctx(world), SESSION, cid)
    assert row(url, "SELECT status FROM mint.self_confirmations WHERE id = %s", cid) == {
        "status": "pending",
    }
    assert row(url, "SELECT count(*) AS n FROM mint.events"
                    " WHERE kind = 'call_approval_approved'")["n"] == 0


def test_consent_rechecks_server_after_service_precheck(world, monkeypatch):
    login(world)
    original = staff._check_consent_target

    def read_then_suspend(*args):
        original(*args)
        with psycopg.connect(world[0]) as admin:
            admin.execute("UPDATE mint.servers SET status = 'suspended'")

    monkeypatch.setattr(staff, "_check_consent_target", read_then_suspend)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        staff.grant_consent(sctx(world), SESSION, **consent_args())
    assert row(world[0], "SELECT count(*) AS n FROM mint.consents")["n"] == 0


@pytest.mark.parametrize("setting", [
    "mint.staff_user_id", "mint.staff_instance_id", "mint.staff_session_id_hash",
    "mint.staff_team_ids",
])
@pytest.mark.parametrize("empty", [False, True])
def test_direct_staff_writes_deny_missing_and_empty_context(world, setting, empty):
    cid = add_confirmation(world[0])
    login(world)
    values = {
        "mint.staff_user_id": USER, "mint.staff_instance_id": INSTANCE,
        "mint.staff_session_id_hash": SESSION, "mint.staff_team_ids": "[]",
    }
    # 새 연결은 GUC 자체가 없는 경우도 시험한다(풀의 빈 값으로 초기화된 경우와 분리).
    with as_login(world[0], "mint_staff") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            for key, value in values.items():
                if key != setting or empty:
                    conn.execute("SELECT set_config(%s, %s, true)",
                                 (key, "" if key == setting else value))
            conn.execute("SELECT mint.staff_confirm(%s, %s)", (cid, CONSOLE_KEY))


def test_direct_staff_write_denies_a_different_instance_and_forged_confirmation_key(world):
    cid = add_confirmation(world[0])
    login(world)
    for context, key in ((replace(sctx(world), instance_id="another-instance"), CONSOLE_KEY),
                         (sctx(world), "forged-console-key")):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with staff._staff_transaction(context, SESSION) as conn:
                conn.execute("SELECT mint.staff_confirm(%s, %s)", (cid, key))


def test_direct_same_key_confirmation_is_refused_by_db_even_if_service_is_skipped(world):
    cid = add_confirmation(world[0])
    login(world, key=HARNESS_KEY)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with staff._staff_transaction(sctx(world), SESSION) as conn:
            conn.execute("SELECT mint.staff_confirm(%s, %s)", (cid, HARNESS_KEY))


def test_team_bindings_in_several_orgs_allow_only_members_of_that_set(world):
    login(world)
    with psycopg.connect(world[0]) as admin:
        admin.execute("DELETE FROM mint.org_bindings WHERE subject_type = 'user'")
        for org in ("beta", "gamma"):
            admin.execute(
                "INSERT INTO mint.orgs (instance_id, org_id, name) VALUES (%s, %s, %s)",
                (INSTANCE, org, org),
            )
            admin.execute(
                "INSERT INTO mint.usage_relations (id, instance_id, org_id, server_id, purpose)"
                " VALUES (gen_random_uuid(), %s, %s, 'srv', 'ops')", (INSTANCE, org),
            )
        for team, org in (("team-a", "acme"), ("team-b", "beta")):
            admin.execute(
                "INSERT INTO mint.org_bindings (id, instance_id, org_id, subject_type, subject_id,"
                " created_by) VALUES (gen_random_uuid(), %s, %s, 'team', %s, 'root')",
                (INSTANCE, org, team),
            )
    context = replace(sctx(world), team_ids=("team-a", "team-b"))
    for org in ("acme", "beta", "gamma"):
        def direct(org=org):
            with staff._staff_transaction(context, SESSION) as conn:
                conn.execute(
                    "SELECT mint.staff_grant_consent(%s, %s, 'srv', 'ops', 'all', 'v1', %s)",
                    (uuid.uuid4(), org, later(days=1)),
                )

        if org == "gamma":
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                direct()
        else:
            direct()


def test_temp_objects_cannot_intercept_staff_function_tables_or_functions(world):
    cid = add_confirmation(world[0])
    login(world)
    with staff._staff_transaction(sctx(world), SESSION) as conn:
        for table in ("console_sessions", "self_confirmations", "org_bindings", "orgs",
                      "settings", "servers", "usage_relations", "events"):
            conn.execute(f"CREATE TEMP TABLE {table} (hijacked text)")
        conn.execute("CREATE FUNCTION pg_temp.staff_user() RETURNS text LANGUAGE sql"
                     " AS $$ SELECT 'attacker'::text $$")
        conn.execute("SELECT mint.staff_confirm(%s, %s)", (cid, CONSOLE_KEY))
        for table in ("console_sessions", "self_confirmations", "org_bindings", "orgs",
                      "settings", "servers", "usage_relations", "events"):
            assert conn.execute(f"SELECT count(*) FROM pg_temp.{table}").fetchone() == (0,)
    assert row(world[0], "SELECT status FROM mint.self_confirmations WHERE id = %s", cid) == {
        "status": "confirmed",
    }


def test_unprivileged_and_plugin_roles_cannot_execute_any_staff_function(world):
    role = "no_staff_" + uuid.uuid4().hex[:12]
    with psycopg.connect(world[0]) as admin:
        admin.execute(f"CREATE ROLE {role} NOLOGIN")
        functions = admin.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'mint' AND p.proname LIKE 'staff\\_%'",
        ).fetchall()
        for subject in (role, "mint_plugin_app"):
            for (function,) in functions:
                assert admin.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                                     (subject, function)).fetchone() == (False,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege), admin.transaction():
                admin.execute(f"SET LOCAL ROLE {subject}")
                admin.execute("SELECT mint.staff_cancel(gen_random_uuid())")
        admin.execute(f"DROP ROLE {role}")


@pytest.mark.parametrize("target", ["rejected", "cancelled", "expired"])
def test_terminal_confirmation_clears_display_with_the_state_change(world, target):
    cid = add_confirmation(world[0], display='{"amount":"private-value"}')
    login(world)
    if target == "expired":
        with psycopg.connect(world[0]) as admin:
            admin.execute("UPDATE mint.self_confirmations SET status = 'expired' WHERE id = %s",
                          (cid,))
    else:
        decide = staff.reject if target == "rejected" else staff.cancel
        decide(sctx(world), SESSION, cid)
    result = row(world[0], "SELECT status, display FROM mint.self_confirmations WHERE id = %s", cid)
    assert result == {"status": target, "display": None}


@pytest.mark.parametrize("days", [None, "", "0", "366"])
def test_direct_consent_denies_unset_empty_or_out_of_range_max_days(world, days):
    login(world)
    with pytest.raises(psycopg.errors.CheckViolation):
        with staff._staff_transaction(sctx(world), SESSION) as conn:
            if days is None:
                conn.execute("RESET mint.staff_consent_max_days")
            else:
                conn.execute("SELECT set_config('mint.staff_consent_max_days', %s, true)", (days,))
            conn.execute(
                "SELECT mint.staff_grant_consent(%s, 'acme', 'srv', 'ops', 'all', 'v1', %s)",
                (uuid.uuid4(), later(days=1)),
            )


# --- 행 잠금: 직원 쓰기와 상태 변경은 직렬화된다 ------------------------------

_CHANGES = {
    "session": "UPDATE mint.console_sessions SET revoked_at = now()",
    "server": "UPDATE mint.servers SET status = 'suspended'",
    "membership": "DELETE FROM mint.org_bindings WHERE subject_type = 'user'",
    "relation": "UPDATE mint.usage_relations SET status = 'terminating'",
    "org": "UPDATE mint.orgs SET status = 'closed'",
}


def _grant(conn):
    conn.execute(
        "SELECT mint.staff_grant_consent(%s, 'acme', 'srv', 'ops', 'metadata', 'v1', %s)",
        (uuid.uuid4(), later(days=1)),
    )


@pytest.mark.parametrize("change", list(_CHANGES))
def test_a_state_change_waits_for_an_open_staff_write(world, change):
    """재확인 직후 철회·조직 이동·중지는 직원 쓰기 트랜잭션이 끝난 뒤에만 들어간다."""
    url = world[0]
    login(world)
    context = sctx(world)
    with staff._staff_transaction(context, SESSION) as conn:
        _grant(conn)
        with psycopg.connect(url, autocommit=True) as other:
            other.execute("SET lock_timeout = '300ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                other.execute(_CHANGES[change])
    with psycopg.connect(url, autocommit=True) as other:
        other.execute(_CHANGES[change])


@pytest.mark.parametrize("change", list(_CHANGES))
def test_a_staff_write_that_waits_on_a_state_change_is_refused_after_it_commits(world, change):
    """상태 변경이 먼저 행을 잡고 있으면 직원 쓰기는 기다렸다가 커밋된 상태로 다시 판정한다."""
    url = world[0]
    login(world)
    context = sctx(world)
    result = {}

    def staff_write():
        try:
            with staff._staff_transaction(context, SESSION) as conn:
                _grant(conn)
            result["error"] = None
        except psycopg.errors.InsufficientPrivilege as exc:
            result["error"] = exc

    with psycopg.connect(url) as admin:
        admin.execute(_CHANGES[change])
        worker = threading.Thread(target=staff_write)
        worker.start()
        worker.join(0.5)
        assert worker.is_alive()  # 변경이 커밋되기 전에는 직원 쓰기가 잠금을 기다린다.
        admin.commit()
    worker.join(10)
    assert isinstance(result["error"], psycopg.errors.InsufficientPrivilege)
