"""운영자 삭제·역할 변경: 권한, 마지막 관리자 보호, 사건, 세션 폐기, 화면."""

from console_fixtures import TOKENS, Keys

from mint_litellm.control.errors import AdminCode


def bearer(role: str) -> dict:
    return {"Authorization": f"Bearer {TOKENS[role]}"}


def version(world, name: str) -> int:
    return world.query("SELECT state_version FROM mint.operators WHERE name = %s", name)[0][0]


def live_users(world):
    return sorted(r[0] for r in world.query(
        "SELECT user_id FROM mint.console_sessions WHERE revoked_at IS NULL"))


def kinds(world):
    return [r[0] for r in world.query(
        "SELECT kind FROM mint.events WHERE kind IN ('operator_removed', 'operator_role_changed')"
        " OR kind = 'console_session_ended' ORDER BY inserted_at")]


def test_removing_an_operator_ends_the_token_and_the_console_session(world):
    approver = world.session_client("approver")
    world.session_client("viewer")
    response = world.client.post(
        "/api/v1/operators/approver/remove", headers=bearer("admin"),
        json={"state_version": version(world, "approver"), "confirm_name": "approver"})
    assert response.status_code == 200, response.text
    assert response.json()["events"][0].endswith(":operator_removed")
    assert approver.get("/").status_code == 303
    assert live_users(world) == ["human-3"]
    assert world.client.get("/api/v1/servers", headers=bearer("approver")).status_code == 401
    listed = world.client.get("/api/v1/operators", headers=bearer("admin")).json()["items"]
    assert {o["name"] for o in listed} == {"admin", "viewer"}
    assert kinds(world) == ["operator_removed", "console_session_ended"]


def test_changing_the_role_ends_the_session_and_applies_the_new_role(world):
    client = world.session_client("approver")
    response = world.client.post(
        "/api/v1/operators/approver/role", headers=bearer("admin"),
        json={"state_version": version(world, "approver"), "role": "viewer"})
    assert response.status_code == 200
    assert client.get("/").status_code == 303
    assert world.query("SELECT role FROM mint.operators WHERE name = 'approver'")[0][0] == "viewer"
    assert "operator_role_changed" in kinds(world)
    again = world.session_client("approver")
    assert again.post("/api/v1/orgs", json={"org_id": "x", "name": "x"},
                      headers={"X-CSRF-Token": world.csrf(again)}).status_code == 403


def test_only_an_admin_may_remove_or_change_operators(world):
    for role in ("viewer", "approver"):
        body = {"state_version": 1, "confirm_name": "viewer"}
        removed = world.client.post("/api/v1/operators/viewer/remove", headers=bearer(role),
                                    json=body)
        assert removed.status_code == 403
        assert removed.json()["code"] == AdminCode.FORBIDDEN
        changed = world.client.post("/api/v1/operators/viewer/role", headers=bearer(role),
                                    json={"state_version": 1, "role": "admin"})
        assert changed.status_code == 403
    assert version(world, "viewer") == 1


def test_the_last_admin_and_oneself_cannot_be_removed_or_demoted(world):
    admin = version(world, "admin")
    own = world.client.post("/api/v1/operators/admin/remove", headers=bearer("admin"),
                            json={"state_version": admin, "confirm_name": "admin"})
    assert own.status_code == 403 and own.json()["code"] == AdminCode.SEPARATION_OF_DUTIES
    demote = world.client.post("/api/v1/operators/admin/role", headers=bearer("admin"),
                               json={"state_version": admin, "role": "viewer"})
    assert demote.status_code == 409 and "마지막 관리자" in demote.json()["message"]
    assert world.query("SELECT role FROM mint.operators WHERE name = 'admin'")[0][0] == "admin"


def test_a_second_admin_may_remove_the_first(world):
    world.query("UPDATE mint.operators SET role = 'admin' WHERE name = 'approver'")
    response = world.client.post(
        "/api/v1/operators/admin/remove", headers=bearer("approver"),
        json={"state_version": version(world, "admin"), "confirm_name": "admin"})
    assert response.status_code == 200


def test_a_stale_version_and_a_wrong_name_are_refused(world):
    stale = world.client.post("/api/v1/operators/viewer/remove", headers=bearer("admin"),
                              json={"state_version": 99, "confirm_name": "viewer"})
    assert stale.status_code == 409 and stale.json()["code"] == AdminCode.CONFLICT
    wrong = world.client.post("/api/v1/operators/viewer/remove", headers=bearer("admin"),
                              json={"state_version": version(world, "viewer"),
                                    "confirm_name": "x"})
    assert wrong.status_code == 400


def test_the_admin_screen_removes_an_operator_with_the_name_typed(world):
    admin = world.session_client("admin")
    html = admin.get("/admin").text
    assert 'data-confirm="viewer"' in html and "운영자 삭제" in html
    data = {"csrf": world.csrf(admin), "state_version": version(world, "viewer")}
    wrong = admin.post("/operators/viewer/remove", data={**data, "confirm_name": "v"})
    assert wrong.status_code == 400 and "이름을 정확히 입력" in wrong.text
    ok = admin.post("/operators/viewer/remove", data={**data, "confirm_name": "viewer"})
    assert ok.status_code == 303 and "operator_removed" in ok.headers["location"]
    assert "viewer</td>" not in admin.get("/admin").text
    assert world.client.post("/login", data={"key": Keys.viewer}).status_code == 403


def test_two_admins_cannot_demote_themselves_at_the_same_time(world):
    import threading
    import time

    import psycopg

    from mint_litellm.control.context import Context, Operator
    from mint_litellm.control.errors import ControlError
    from mint_litellm.control.services import operators as service
    from mint_litellm.types.cli_commands import OperatorRole

    world.query("UPDATE mint.operators SET role = 'admin' WHERE name = 'approver'")
    first = psycopg.connect(world.admin_url)
    second = psycopg.connect(world.admin_url)
    ctx_a = Context(first, "inst1", Operator("admin", "admin", "human-1"))
    ctx_b = Context(second, "inst1", Operator("approver", "admin", "human-2"))
    outcome: list[object] = []

    def other():
        try:
            service.set_role(ctx_b, "approver", OperatorRole.VIEWER)
            outcome.append("changed")
        except ControlError as error:
            outcome.append(error)

    thread = threading.Thread(target=other)
    with first.transaction():  # 바깥 트랜잭션이 끝날 때까지 잠금을 쥔다
        service.set_role(ctx_a, "admin", OperatorRole.VIEWER)
        thread.start()
        time.sleep(1.0)
        assert outcome == []  # 둘째는 잠금에서 기다린다
    thread.join(10)
    first.close()
    second.close()
    assert len(outcome) == 1 and isinstance(outcome[0], ControlError)
    assert "마지막 관리자" in str(outcome[0])
    roles = world.query("SELECT name, role FROM mint.operators WHERE role = 'admin'")
    assert roles == [("approver", "admin")]
