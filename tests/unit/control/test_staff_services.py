"""직원 서비스: 실제 직원 로그인(표를 읽지 못함)과 제어 로그인(확인용 읽기)으로 검증한다.
MINT_TEST_DATABASE_URL이 없으면 건너뛴다."""

import uuid
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
    return staff.StaffContext(staff_conn, control_conn, INSTANCE, user)


def add_confirmation(url, user=USER, *, expires=None, requested_key=HARNESS_KEY, tool="echo"):
    cid = uuid.uuid4()
    with psycopg.connect(url) as admin:
        admin.execute(
            "INSERT INTO mint.self_confirmations (id, instance_id, org_id, principal_id,"
            " server_id, tool, args_fp, fp_key_version, policy_version_id, purpose,"
            " requested_by_key_token, expires_at) VALUES (%s, %s, 'acme', %s, 'srv', %s, %s, 1,"
            " 'v1', '결제', %s, %s)",
            (cid, INSTANCE, user, tool, HEX, requested_key, expires or later(minutes=10)),
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
                  text_version="v1", valid_until=later(days=30), max_valid_days=90)
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
    with pytest.raises(ControlError, match="suspended"):
        staff.grant_consent(sctx(world), SESSION, **consent_args())
