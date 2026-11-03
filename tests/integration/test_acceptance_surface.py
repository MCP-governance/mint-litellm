"""1단계 인수시험: LiteLLM 접점, 서버 식별, 설정 점검, 조직 경계, 증거 위치, 정책 전환.

구성요소가 병합되기 전에는 건너뛴다. 기대값은 확정된 동작을 그대로 옮긴 것이다.
"""

import json
import os
import re
from pathlib import Path

import httpx
import pytest
import support
from scenario import Outcome, decision, litellm_config
from support import mcp_session, run
from test_acceptance_core import assert_allowed, assert_blocked, code_of

pytestmark = [pytest.mark.needs_plugin, pytest.mark.needs_control]

CONFIG_DIR = Path(__file__).resolve().parent / "config"


@pytest.fixture
def approved(world, evidence):
    evidence.positive_control()
    subject = world.subject(("a", "b"))
    relation = world.prepare(subject)
    world.approve(subject, relation)
    return subject, relation


def test_tools_list_records_one_event_per_tool_and_one_upstream_list_per_server(
    world, approved, proxy_log
):
    subject, _ = approved
    since = world.now()
    proxy_log.mark()

    async def listing():
        async with mcp_session(subject.key) as session:
            return (await session.list_tools()).tools

    tools = run(listing())
    assert len(tools) == 6
    assert world.count_since(since, "discovery_evaluated") == len(tools)
    for server in ("a", "b"):
        lists = [r for r in proxy_log.rows(rpc="tools/list") if r["path"].startswith(f"/{server}/")]
        assert len(lists) == 1


def test_rest_call_records_exactly_one_decision(world, evidence, approved):
    subject, _ = approved
    assert_allowed(world, evidence, world.call(subject))


def test_duplicate_call_id_yields_two_events_with_distinct_invocations(world, evidence, approved):
    subject, _ = approved
    mark = support.marker()
    first = world.call(subject, mark=mark)
    second = world.call(subject, mark=mark)
    assert first.allowed and second.allowed
    events = world.events(mark)
    assert len(events) == 2
    assert len({e["payload"]["invocation_id"] for e in events}) == 2


def test_prompts_and_resources_leave_no_event_and_are_not_blocked(world, approved):
    subject, _ = approved
    since = world.now()

    async def touch():
        async with mcp_session(subject.key) as session:
            prompts = (await session.list_prompts()).prompts
            await session.get_prompt(
                next(p.name for p in prompts if p.name.endswith("greeting")), {}
            )
            resources = (await session.list_resources()).resources
            await session.read_resource(resources[0].uri)

    run(touch())
    for kind in ("decision_evaluated", "discovery_evaluated"):
        assert world.count_since(since, kind) == 0


def test_master_key_call_is_forbidden(world, evidence):
    evidence.positive_control()
    mark = support.marker()
    reply = support.rest_call(
        support.MASTER_KEY, "a", "echo", {"text": "x", "marker": mark}, call_id=mark
    )
    assert reply.status_code == 403
    assert code_of("MASTER_KEY_FORBIDDEN") in reply.text
    evidence.assert_calls(mark, 0)


def test_guardrail_optout_key_is_still_blocked_by_first_enforcement_point(world, evidence):
    evidence.positive_control()
    subject = world.subject(("a",), metadata={"disable_global_guardrails": True})
    world.prepare(subject)
    assert_blocked(world, evidence, world.call(subject), "APPROVAL_ABSENT")


def test_startup_stops_when_logger_is_not_registered():
    source = Path(os.environ.get("MINT_E_MINT_CONFIG", CONFIG_DIR / "litellm.mint.yaml"))
    text = re.sub(r"(?m)^.*MintLogger.*\n", "", source.read_text(encoding="utf-8"))
    variant = CONFIG_DIR / "litellm.nologger.generated.yaml"
    variant.write_text(text, encoding="utf-8")
    try:
        done = support.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "litellm1",
            check=False,
            env={"MINT_E_LITELLM_CONFIG": str(variant)},
        )
        assert done.returncode != 0
    finally:
        variant.unlink(missing_ok=True)


def run_check(world) -> tuple[int, dict]:
    done = world.mint.run("check", check=False)
    return done.returncode, json.loads(done.stdout)


def test_check_observes_spend_log_arguments(world):
    code, report = run_check(world)
    assert report["observed"]["spend_log_arguments_stored"] is True
    assert code == 0


def test_check_fails_when_prompts_are_stored_in_spend_logs(world):
    source = Path(os.environ.get("MINT_E_MINT_CONFIG", CONFIG_DIR / "litellm.mint.yaml"))
    text = source.read_text(encoding="utf-8").replace(
        "general_settings:", "general_settings:\n  store_prompts_in_spend_logs: true", 1
    )
    variant = CONFIG_DIR / "litellm.spend.generated.yaml"
    variant.write_text(text, encoding="utf-8")
    try:
        with litellm_config(str(variant)):
            code, _ = run_check(world)
            assert code != 0
    finally:
        variant.unlink(missing_ok=True)


def test_check_warns_once_without_personal_key_role_restriction(world):
    source = Path(os.environ.get("MINT_E_MINT_CONFIG", CONFIG_DIR / "litellm.mint.yaml"))
    text = re.sub(
        r"(?ms)^  key_generation_settings:.*?(?=^\S|\Z)", "", source.read_text(encoding="utf-8")
    )
    variant = CONFIG_DIR / "litellm.norole.generated.yaml"
    variant.write_text(text, encoding="utf-8")
    try:
        with litellm_config(str(variant)):
            _, report = run_check(world)
            assert len([w for w in report["warnings"] if "allowed_user_roles" in w]) == 1
    finally:
        variant.unlink(missing_ok=True)


def test_check_counts_optout_subjects_and_unbound_keys(world):
    before = run_check(world)[1]["observed"]
    support.new_key(("a",), metadata={"disable_global_guardrails": True})
    support.new_key(("a",))
    after = run_check(world)[1]["observed"]
    assert after["guardrail_optout_subjects"] == before["guardrail_optout_subjects"] + 1
    assert after["unbound_keys"] >= before["unbound_keys"] + 2


def test_unbound_key_is_blocked_as_anonymous(world, evidence):
    evidence.positive_control()
    key = support.new_key(("a",))
    mark = support.marker()
    reply = support.rest_call(key, "a", "echo", {"text": "x", "marker": mark}, call_id=mark)
    assert reply.status_code == 403 and code_of("ANONYMOUS") in reply.text
    evidence.assert_calls(mark, 0)


def test_control_plane_key_reaches_only_its_routes(world):
    key = world.control_key
    headers = {"Authorization": f"Bearer {key}"}
    assert (
        httpx.get(
            support.url(support.LITELLM_PORTS[0], "/v1/mcp/server"), headers=headers
        ).status_code
        == 200
    )
    assert (
        httpx.post(
            support.url(support.LITELLM_PORTS[0], "/key/generate"), headers=headers, json={}
        ).status_code
        == 403
    )
    assert (
        httpx.post(
            support.url(support.LITELLM_PORTS[0], "/mcp"),
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        ).status_code
        == 403
    )
    world.mint.run("server", "register", support.SERVER_ID["b"], check=False)


def test_team_without_binding_and_no_default_org_is_org_boundary(world, evidence):
    evidence.positive_control()
    world.mint.run("settings", "default-org", "--clear")
    subject = world.subject(team=True)
    world.register()
    outcome = world.call(subject)
    assert_blocked(world, evidence, outcome, "ORG_BOUNDARY")


def test_approval_of_another_organization_is_org_boundary(world, evidence):
    evidence.positive_control()
    owner = world.subject(team=True)
    relation_a = world.prepare(owner)
    world.approve(owner, relation_a)
    other = world.subject(team=True)
    world.prepare(other)
    assert_blocked(world, evidence, world.call(other), "ORG_BOUNDARY")


def test_binding_one_team_to_two_organizations_is_rejected(world):
    subject = world.subject(team=True)
    world.prepare(subject)
    world.mint.run("org", "add", "org-second")
    done = world.mint.run("org", "bind", "org-second", "--team", subject.team_id, check=False)
    assert done.returncode != 0


def test_same_alias_in_db_server_is_identity_ambiguous(world, evidence):
    evidence.positive_control()
    created = httpx.post(
        support.url(support.LITELLM_PORTS[0], "/v1/mcp/server"),
        headers=support.master_headers(),
        json={
            "alias": "mcp_a",
            "server_name": "mcp_a",
            "url": "http://counting-proxy:8080/a/mcp",
            "transport": "http",
            "allow_all_keys": False,
        },
        timeout=30,
    )
    created.raise_for_status()
    db_server = created.json()["server_id"]
    try:
        key = support.new_key(())
        httpx.post(
            support.url(support.LITELLM_PORTS[0], "/key/update"),
            headers=support.master_headers(),
            json={"key": key, "object_permission": {"mcp_servers": [db_server]}},
            timeout=30,
        )
        mark = support.marker()
        reply = support.rest_call(key, "a", "echo", {"text": "x", "marker": mark}, call_id=mark)
        assert reply.status_code == 403
        assert code_of("SERVER_IDENTITY_AMBIGUOUS") in reply.text
        evidence.assert_calls(mark, 0)
    finally:
        httpx.delete(
            support.url(support.LITELLM_PORTS[0], f"/v1/mcp/server/{db_server}"),
            headers=support.master_headers(),
            timeout=30,
        )


def test_changed_server_url_is_identity_mismatch(world, evidence):
    evidence.positive_control()
    base = support.url(support.LITELLM_PORTS[0], "/v1/mcp/server")
    created = httpx.post(
        base,
        headers=support.master_headers(),
        timeout=30,
        json={
            "alias": "mcp_db",
            "server_name": "mcp_db",
            "url": "http://counting-proxy:8080/a/mcp",
            "transport": "http",
            "allow_all_keys": False,
        },
    )
    created.raise_for_status()
    server_id = created.json()["server_id"]
    try:
        key = support.new_key(())
        httpx.post(
            support.url(support.LITELLM_PORTS[0], "/key/update"),
            headers=support.master_headers(),
            json={"key": key, "object_permission": {"mcp_servers": [server_id]}},
            timeout=30,
        )
        world.mint.run("server", "register", server_id)
        httpx.put(
            base,
            headers=support.master_headers(),
            timeout=30,
            json={"server_id": server_id, "url": "http://counting-proxy:8080/b/mcp"},
        ).raise_for_status()
        mark = support.marker()
        reply = httpx.post(
            support.url(support.LITELLM_PORTS[0], "/mcp-rest/tools/call"),
            headers={"Authorization": f"Bearer {key}", "x-litellm-call-id": mark},
            timeout=60,
            json={
                "server_id": server_id,
                "name": "mcp_db-echo",
                "arguments": {"text": "x", "marker": mark},
            },
        )
        assert reply.status_code == 403
        assert any(
            code_of(r) in reply.text for r in ("SERVER_IDENTITY_MISMATCH", "SERVER_UNREGISTERED")
        )
        evidence.assert_calls(mark, 0, "a")
        evidence.assert_calls(mark, 0, "b")
    finally:
        httpx.delete(f"{base}/{server_id}", headers=support.master_headers(), timeout=30)


def test_alias_form_permission_is_rejected_by_approve(world):
    subject = world.subject(("a",))
    relation = world.prepare(subject)
    key = support.new_key(())
    httpx.post(
        support.url(support.LITELLM_PORTS[0], "/key/update"),
        headers=support.master_headers(),
        json={"key": key, "object_permission": {"mcp_servers": ["mcp_a"]}},
        timeout=30,
    )
    done = world.mint.run(
        "approve",
        "--relation",
        relation,
        "--key",
        key,
        "--basis",
        "integration-evidence",
        check=False,
    )
    assert done.returncode != 0


def count_buffer_events(mark: str) -> int:
    done = support.compose(
        "exec",
        "-T",
        "litellm1",
        "sh",
        "-c",
        f"grep -rh '{mark}' /var/lib/mint/buffer | wc -l",
        check=False,
    )
    return int(done.stdout.strip() or 0)


def test_evidence_normal_path_has_db_row_and_no_buffer_row(world, evidence, approved):
    subject, _ = approved
    outcome = world.call(subject)
    assert len(world.events(outcome.mark)) == 1
    assert count_buffer_events(outcome.mark) == 0


def test_evidence_database_down_leaves_one_buffer_row(world, evidence, approved, toxics):
    subject, _ = approved
    toxics["postgres"].disable()
    try:
        outcome = world.call(subject)
        assert outcome.status == 403
        assert count_buffer_events(outcome.mark) == 1
    finally:
        toxics["postgres"].enable()


def test_evidence_response_loss_after_commit_converges_after_replay(
    world, evidence, approved, toxics
):
    subject, _ = approved
    toxics["postgres"].add("timeout", stream="downstream", timeout=0)
    try:
        outcome = world.call(subject)
    finally:
        toxics["postgres"].clear()
    key = outcome.mark
    assert len(world.events(key)) == 1
    assert count_buffer_events(key) == 1
    support.wait_until(lambda: world.events(key, "buffer_replayed") or True, what="replay")
    assert len(world.events(key)) == 1
    assert world.events(key, "buffer_replayed")


def test_evidence_database_down_and_buffer_read_only_goes_to_stderr(
    world, evidence, approved, toxics
):
    subject, _ = approved
    override = Path(__file__).resolve().parent / "compose.buffer-readonly.yaml"
    support.compose("-f", str(override), "up", "-d", "--wait", "--force-recreate", "litellm1")
    toxics["postgres"].disable()
    try:
        outcome = world.call(subject)
        assert outcome.status == 403
        logs = support.compose("logs", "--no-log-prefix", "litellm1").stdout
        lines = [x for x in logs.splitlines() if "decision_evaluated" in x and "stderr" in x]
        assert len(lines) == 1
    finally:
        toxics["postgres"].enable()
        support.compose("up", "-d", "--wait", "--force-recreate", "litellm1")


@pytest.mark.needs_policy
def test_observe_mode_policy_ids_filter_converts_only_observable_policy(world, evidence):
    """R3(업무 시간 밖 Block)·R2(고위험 도구 RequireApproval)·R4(동의 미보유) 동시 적용.

    관측 정책 ID가 R1·R3이면 정책 출력은 policy_ids=[R3]이고 관측 가능이므로 허용으로 전환된다.
    """
    evidence.positive_control()
    world.mint.run("settings", "mode", "observe")
    world.mint.run("settings", "observable", "--policy-ids", "R1,R3")
    world.mint.run("settings", "high-risk-tools", "--tools", "echo")
    subject = world.subject()
    world.approve(subject, world.prepare(subject))
    try:
        outcome: Outcome = world.call(subject, mark=support.marker())
        events = world.events(outcome.mark)
        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["policy_ids"] == ["R3"]
        assert decision(events[0])[:2] == ("block", "allow")
        evidence.assert_calls(outcome.mark, 1)
    finally:
        world.mint.run("settings", "mode", "enforce")
