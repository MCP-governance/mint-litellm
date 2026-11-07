"""실제 LiteLLM(MINT_TEST_LITELLM_URL, MINT_TEST_LITELLM_KEY=마스터 키)에 대한 권한 동기화 실측.

모의 서버가 아니라 실제 관리 API의 키·팀·서버 동작을 쓴다. 환경 변수가 없으면 건너뛴다.
"""

import hashlib
import json
import os
import urllib.error
import urllib.request
import uuid

import pytest
from ctl_fixtures import INSTANCE

from . import test_sync
from .test_sync import (
    SETTINGS,
    add_approval,
    add_server,
    alerts,
    drain,
    enqueue,
    events,
    grant,
    job_rows,
    login_url,
    reconcile,
    revoke,
)

admin = test_sync.admin  # 장치(같은 이름으로 다시 내보낸다)
url = test_sync.url

psycopg = pytest.importorskip("psycopg")

from mint_litellm.control.litellm_write import LiteLLMSync  # noqa: E402
from mint_litellm.control.worker import Worker  # noqa: E402

BASE = os.environ.get("MINT_TEST_LITELLM_URL")
MASTER = os.environ.get("MINT_TEST_LITELLM_KEY")
pytestmark = pytest.mark.skipif(not (BASE and MASTER), reason="실제 LiteLLM 환경 변수 없음")


def call(method: str, path: str, body=None, key: str | None = None):
    request = urllib.request.Request(
        f"{BASE}{path}", method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key or MASTER}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


@pytest.fixture
def real_worker(url, admin):
    made = []

    def make() -> Worker:
        worker = Worker(login_url(url), INSTANCE, SETTINGS, litellm=LiteLLMSync(BASE, MASTER))
        worker.open()
        admin.execute("DELETE FROM mint.schedules")
        made.append(worker)
        return worker

    yield make()
    for worker in made:
        worker.close()


def tag() -> str:
    return uuid.uuid4().hex[:8]


def new_server(name: str) -> str:
    status, body = call("POST", "/v1/mcp/server", {
        "server_name": name, "alias": name, "url": "http://upstream.invalid/mcp",
        "transport": "http"})
    assert status == 201, body
    return body["server_id"]


def new_team_key(team_id: str, user: str) -> str:
    assert call("POST", "/team/new", {"team_id": team_id, "team_alias": team_id})[0] == 200
    status, body = call("POST", "/key/generate", {"user_id": user, "team_id": team_id})
    assert status == 200
    return body["key"]


def key_permission(token: str) -> tuple[list, dict]:
    status, body = call("GET", f"/key/info?key={token}")
    perm = body["info"].get("object_permission") or {}
    return perm.get("mcp_servers") or [], perm.get("mcp_tool_permissions") or {}


def test_a_key_without_a_team_is_synced_confirmed_and_revoked_on_real_litellm(
    admin, real_worker
):
    server_id = new_server(f"s_{tag()}")
    status, body = call("POST", "/key/generate", {"user_id": f"u-{tag()}"})
    token = body["key"]
    add_server(admin, server_id)
    approval = add_approval(admin, "key", token, server_id=server_id, tools=("t1", "t2"))
    enqueue(admin, "sync_target", f"key:{token}")
    drain(real_worker)
    assert key_permission(token) == ([server_id], {server_id: ["t1", "t2"]})
    assert grant(admin, "mcp_server", target=token, server=server_id)["observed"] is True
    assert alerts(admin) == []  # 응답의 비소유 필드가 읽기 전 값과 같다
    assert len(events(admin, "sync_applied")) == 2
    revoke(admin, approval)
    enqueue(admin, "sync_target", f"key:{token}")
    drain(real_worker)
    assert key_permission(token) == ([], {})
    assert alerts(admin) == []


def test_the_hashed_key_form_works_for_update_and_read(admin, real_worker):
    server_id = new_server(f"s_{tag()}")
    token = call("POST", "/key/generate", {"user_id": f"u-{tag()}"})[1]["key"]
    hashed = hashlib.sha256(token.encode()).hexdigest()
    add_server(admin, server_id)
    add_approval(admin, "key", hashed, server_id=server_id, tools=("t1",))
    enqueue(admin, "sync_target", f"key:{hashed}")
    drain(real_worker)
    assert key_permission(token) == ([server_id], {server_id: ["t1"]})


def test_a_team_key_gets_403_until_the_team_allows_the_server(admin, real_worker):
    server_id = new_server(f"s_{tag()}")
    team = f"team-{tag()}"
    token = new_team_key(team, f"u-{tag()}")
    add_server(admin, server_id)
    add_approval(admin, "team", team, server_id=server_id, tools=("t1",))
    add_approval(admin, "key", token, server_id=server_id, tools=("t1",))
    enqueue(admin, "sync_target", f"key:{token}")
    drain(real_worker)
    [key_job] = [j for j in job_rows(admin, "sync_target") if j["target_ref"].startswith("key:")]
    assert (key_job["status"], key_job["last_error_code"]) == ("failed", "litellm_forbidden")
    status, body = call("GET", f"/team/info?team_id={team}")
    assert body["team_info"]["object_permission"]["mcp_servers"] == [server_id]
    reconcile(admin)
    drain(real_worker)
    assert key_permission(token) == ([server_id], {server_id: ["t1"]})
    assert [a["status"] for a in alerts(admin)] == ["resolved"]  # 403 때 연 경보가 닫혔다


def test_a_submitted_server_is_approved_and_the_approval_activates_on_real_litellm(
    admin, real_worker
):
    team = f"team-{tag()}"
    submitter = new_team_key(team, f"u-{tag()}")
    name = f"sub_{tag()}"
    status, body = call("POST", "/v1/mcp/server/register", {
        "server_name": name, "alias": name, "url": "http://upstream.invalid/mcp",
        "transport": "http"}, key=submitter)
    assert status == 201 and body["approval_status"] == "pending_review", body
    server_id = body["server_id"]
    add_server(admin, server_id)
    approval = add_approval(admin, "team", team, server_id=server_id, tools=("t1",),
                            status="pending")
    enqueue(admin, "sync_server", server_id)
    enqueue(admin, "sync_target", f"team:{team}")
    drain(real_worker)
    status, body = call("GET", f"/v1/mcp/server/{server_id}")
    assert body["approval_status"] == "active"
    assert psycopg_status(admin, approval) == "active"
    assert alerts(admin) == []


def psycopg_status(conn, approval_id):
    return conn.execute("SELECT status FROM mint.approvals WHERE id = %s",
                        (approval_id,)).fetchone()[0]


def test_an_external_deletion_is_restored_once_and_held_the_second_time(admin, real_worker):
    server_id = new_server(f"s_{tag()}")
    token = call("POST", "/key/generate", {"user_id": f"u-{tag()}"})[1]["key"]
    add_server(admin, server_id)
    add_approval(admin, "key", token, server_id=server_id, tools=("t1",))
    enqueue(admin, "sync_target", f"key:{token}")
    drain(real_worker)
    for expected_held in (False, True):
        call("POST", "/key/update", {"key": token, "object_permission": {
            "mcp_servers": [], "mcp_tool_permissions": {server_id: ["t1"]}}})
        reconcile(admin)
        drain(real_worker)
        row = grant(admin, "mcp_server", target=token, server=server_id)
        assert row["held"] is expected_held
        assert (key_permission(token)[0] == []) is expected_held
    assert len(events(admin, "sync_held")) == 1


def test_a_server_opened_to_all_keys_is_closed_with_the_minimal_body(admin, real_worker):
    name = f"open_{tag()}"
    status, body = call("POST", "/v1/mcp/server", {
        "server_name": name, "alias": name, "url": "http://upstream.invalid/mcp",
        "transport": "http", "allow_all_keys": True})
    assert status == 201 and body["allow_all_keys"] is True, body
    server_id = body["server_id"]
    add_server(admin, server_id)
    add_approval(admin, "user", f"u_{tag()}", server_id=server_id)
    reconcile(admin)
    drain(real_worker)
    status, body = call("GET", f"/v1/mcp/server/{server_id}")
    assert body["allow_all_keys"] is False
    assert body["url"] == "http://upstream.invalid/mcp"  # 다른 필드는 그대로
    assert [a["kind"] for a in alerts(admin)] == ["sync_foreign_change"]


def managed_real_server(admin, definitions=None) -> tuple[str, dict]:
    """실제 LiteLLM에 서버를 만들고 지금 고정 응답을 승인 계약으로 삼는다."""
    from .test_sync_pin import add_contract

    name = f"pin_{tag()}"
    status, body = call("POST", "/v1/mcp/server", {
        "server_name": name, "alias": name, "url": os.environ.get(
            "MINT_TEST_UPSTREAM_URL", "http://mint-g4-ts:9000/mcp"), "transport": "http"})
    assert status == 201, body
    server_id = body["server_id"]
    status, pinned = call("POST", f"/v1/mcp/server/{server_id}/pin")
    assert status == 200 and pinned, pinned  # 실측: {도구: {description, input_schema}}
    call("DELETE", f"/v1/mcp/server/{server_id}/pin")
    add_server(admin, server_id)
    admin.execute("UPDATE mint.servers SET contract_status = 'managed'")
    for tool, value in (definitions or pinned).items():
        add_contract(admin, tool, value, server=server_id)
    return server_id, pinned


@pytest.mark.skipif(not os.environ.get("MINT_TEST_UPSTREAM_URL")
                    and not os.environ.get("MINT_TEST_PIN"), reason="업스트림 MCP 서버 필요")
def test_a_real_pin_matches_the_approved_contracts_and_activates(admin, real_worker):
    server_id, pinned = managed_real_server(admin)
    approval = add_approval(admin, "user", f"u_{tag()}", server_id=server_id, status="pending")
    enqueue(admin, "sync_server", server_id)
    drain(real_worker)
    row = grant(admin, "pin", "server", server_id, server_id)
    from mint_litellm.types.canonical import pin_set_sha256

    assert (row["observed"], row["applied_sha256"]) == (True, pin_set_sha256(pinned))
    assert admin.execute("SELECT status FROM mint.approvals WHERE id = %s",
                         (approval,)).fetchone()[0] == "active"
    assert alerts(admin) == []


@pytest.mark.skipif(not os.environ.get("MINT_TEST_UPSTREAM_URL")
                    and not os.environ.get("MINT_TEST_PIN"), reason="업스트림 MCP 서버 필요")
def test_a_real_pin_with_a_changed_definition_is_unpinned_and_reported(admin, real_worker):
    server_id, pinned = managed_real_server(admin)
    admin.execute("SET session_replication_role = replica")
    admin.execute("UPDATE mint.tool_contracts SET pin_sha256 = %s WHERE tool = 'echo'", ("0" * 64,))
    admin.execute("SET session_replication_role = origin")
    approval = add_approval(admin, "user", f"u_{tag()}", server_id=server_id, status="pending")
    enqueue(admin, "sync_server", server_id)
    drain(real_worker)
    assert [a["subject"]["tools"] for a in alerts(admin)] == [["echo"]]
    assert call("GET", f"/v1/mcp/server/{server_id}")[1]["pinned_tools"] in (None, {}, [])
    assert admin.execute("SELECT status FROM mint.approvals WHERE id = %s",
                         (approval,)).fetchone()[0] == "pending"
