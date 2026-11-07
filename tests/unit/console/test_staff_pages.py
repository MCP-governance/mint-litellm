"""직원 화면: 로그인(운영자 행 없이)·세션·소속 팀·화면 11~13·거부 경우.

직원 연결은 실제 ``mint_staff_app`` 로그인으로 접속하므로 표를 직접 읽거나 쓰지 못한다.
"""

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from console_fixtures import CONTROL_KEY, token_of
from ctl_fixtures import INSTANCE, as_login

from mint_litellm.console.core import COOKIE, STAFF_COOKIE
from mint_litellm.control import jobs, session_verify
from mint_litellm.control.litellm_admin import LiteLLMAdmin

HEX = "ab" * 32
STAFFER = "human-4"
OTHER = "human-5"
HARNESS = "sk-staff-harness"
CONSOLE = "sk-staff-console"
OTHER_KEY = "sk-other-staff"


@pytest.fixture
def sworld(world):
    """운영자가 아닌 직원 두 명(같은 조직)과 본인 확인에 필요한 정책 판본이 있는 배치."""
    for key, user in ((HARNESS, STAFFER), (CONSOLE, STAFFER), (OTHER_KEY, OTHER)):
        world.stub.own(key, user)
        world.stub.control(key, user)
    with psycopg.connect(world.admin_url) as admin:
        for user in (STAFFER, OTHER):
            admin.execute(
                "INSERT INTO mint.org_bindings (id, instance_id, org_id, subject_type,"
                " subject_id, created_by) VALUES (gen_random_uuid(), %s, 'acme', 'user', %s,"
                " 'root')",
                (INSTANCE, user),
            )
        admin.execute(
            "INSERT INTO mint.policy_versions (instance_id, id, catalog_sha256, bundle_sha256)"
            " VALUES (%s, 'v1', %s, %s)",
            (INSTANCE, HEX, HEX),
        )
    return world


def staff_login(world, key: str):
    client = world.client.__class__(
        world.app, base_url="https://testserver", follow_redirects=False
    )
    response = client.post("/staff/login", data={"key": key})
    return client, response


def logged_in(world, key: str = CONSOLE):
    client, response = staff_login(world, key)
    assert response.status_code == 303, response.text
    return client


def csrf(client) -> str:
    from mint_litellm.control.services.console_sessions import csrf_token

    return csrf_token(client.cookies[STAFF_COOKIE])


def add_confirmation(world, user=STAFFER, *, requested_key=HARNESS, tool="send_mail",
                     display=None, minutes=10):
    cid = uuid.uuid4()
    with psycopg.connect(world.admin_url) as admin:
        admin.execute(
            "INSERT INTO mint.self_confirmations (id, instance_id, org_id, principal_id,"
            " server_id, tool, args_fp, fp_key_version, policy_version_id, purpose,"
            " requested_by_key_token, expires_at, display)"
            " VALUES (%s, %s, 'acme', %s, 'srv-ok', %s, %s, 1, 'v1', '결제',"
            " %s, %s, %s::jsonb)",
            (cid, INSTANCE, user, tool, HEX, token_of(requested_key),
             datetime.now(UTC) + timedelta(minutes=minutes), display),
        )
    return str(cid)


def decide(client, cid: str, decision: str):
    return client.post(f"/staff/confirmations/{cid}/{decision}", data={"csrf": csrf(client)})


def status_of(world, cid: str) -> str:
    return world.query("SELECT status FROM mint.self_confirmations WHERE id = %s", cid)[0][0]


# --- 로그인·세션 ------------------------------------------------------------------------------


def test_a_human_who_is_not_an_operator_logs_in_to_the_staff_screens(sworld):
    client, response = staff_login(sworld, CONSOLE)
    assert response.status_code == 303 and response.headers["location"] == "/staff"
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("__host-mint_staff=")
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    rows = sworld.query("SELECT subject, user_id, role, operator_name, key_token"
                        " FROM mint.console_sessions")
    assert rows == [("staff", STAFFER, "staff", None, token_of(CONSOLE))]
    assert client.get("/staff").status_code == 200


def test_the_same_key_cannot_open_the_admin_console_without_an_operator_row(sworld):
    response = sworld.client.post("/login", data={"key": CONSOLE})
    assert response.status_code == 403


@pytest.mark.parametrize("key, text", [
    ("sk-unknown", "키를 확인할 수 없습니다"),
    ("", "키를 입력하십시오"),
])
def test_staff_login_refuses_unknown_and_empty_keys(sworld, key, text):
    client, response = staff_login(sworld, key)
    assert response.status_code == 401 and text in response.text
    assert STAFF_COOKIE not in response.cookies


def test_a_service_account_key_without_a_user_cannot_log_in(sworld):
    sworld.stub.own("sk-service", None)
    _, response = staff_login(sworld, "sk-service")
    assert response.status_code == 401


def test_staff_login_writes_a_session_event_and_stores_no_plain_key(sworld):
    logged_in(sworld)
    kinds = sworld.query("SELECT kind, actor, payload->>'subject' FROM mint.events"
                         " WHERE kind LIKE 'console_session_%'")
    assert kinds == [("console_session_started", STAFFER, "staff")]
    stored = sworld.query(
        "SELECT (SELECT string_agg(t::text, ' ') FROM mint.console_sessions t) || ' ' ||"
        " (SELECT string_agg(e::text, ' ') FROM mint.events e)")[0][0]
    assert CONSOLE not in stored and token_of(CONSOLE) in stored


def test_the_staff_cookie_does_not_open_admin_pages_or_the_api(sworld):
    client = logged_in(sworld)
    session_id = client.cookies[STAFF_COOKIE]
    for path in ("/", "/servers", "/approvals", "/ledger", "/admin"):
        response = client.get(path)
        assert response.status_code == 303 and response.headers["location"] == "/login"
    assert client.get("/api/v1/servers").status_code == 401
    forged = client.__class__(sworld.app, base_url="https://testserver", follow_redirects=False)
    forged.cookies.set(COOKIE, session_id)
    assert forged.get("/").status_code == 303
    assert forged.get("/api/v1/servers").status_code == 401


def test_the_admin_cookie_does_not_open_staff_screens(sworld):
    admin = sworld.session_client("admin")
    session_id = admin.cookies[COOKIE]
    assert admin.get("/staff").status_code == 303
    admin.cookies.set(STAFF_COOKIE, session_id)
    response = admin.get("/staff")
    assert response.status_code == 303 and response.headers["location"] == "/staff/login"


def test_logout_revokes_the_staff_session_and_records_it(sworld):
    client = logged_in(sworld)
    response = client.post("/staff/logout", data={"csrf": csrf(client)})
    assert response.status_code == 303 and response.headers["location"] == "/staff/login"
    assert client.get("/staff").status_code == 303
    ended = sworld.query("SELECT actor FROM mint.events WHERE kind = 'console_session_ended'")
    assert ended == [(STAFFER,)]


def test_a_staff_request_without_a_csrf_token_is_refused(sworld):
    client = logged_in(sworld)
    cid = add_confirmation(sworld, requested_key=HARNESS)
    assert client.post(f"/staff/confirmations/{cid}/reject").status_code == 403
    assert status_of(sworld, cid) == "pending"


# --- 소속 팀 ----------------------------------------------------------------------------------


def test_teams_come_from_the_key_info_and_the_user_info_never_from_the_request(sworld):
    sworld.stub.own("sk-team", STAFFER, team_id="team-key")
    sworld.stub.routes[("/user/info", "sk-team", None)] = (
        200, {"user_id": STAFFER, "keys": [], "user_info": {
            "user_id": STAFFER, "user_role": "internal_user", "teams": ["team-user", 7]}})
    logged_in(sworld, "sk-team")
    teams = sworld.query("SELECT team_ids FROM mint.console_sessions")[0][0]
    assert teams == ["team-key", "team-user"]


def test_a_key_without_a_team_has_an_empty_team_list(sworld):
    logged_in(sworld)
    assert sworld.query("SELECT team_ids FROM mint.console_sessions") == [([],)]


def test_session_verification_refreshes_the_teams_of_a_staff_session(sworld):
    logged_in(sworld)
    sworld.stub.control(CONSOLE, STAFFER, team_id="team-new")
    run_verify(sworld)
    assert sworld.query("SELECT team_ids FROM mint.console_sessions") == [(["team-new"],)]


def test_session_verification_revokes_a_staff_session_of_a_blocked_key(sworld):
    client = logged_in(sworld)
    sworld.stub.control(CONSOLE, STAFFER, blocked=True)
    run_verify(sworld)
    assert sworld.query("SELECT revoked_at IS NOT NULL FROM mint.console_sessions") == [(True,)]
    assert client.get("/staff").status_code == 303


def run_verify(world):
    conn = as_login(world.admin_url, "mint_worker")
    conn.autocommit = True
    try:
        litellm = LiteLLMAdmin(world.stub.url, CONTROL_KEY)
        session_verify.run(jobs.JobContext(conn, INSTANCE, {}, None, litellm))
    finally:
        conn.close()

