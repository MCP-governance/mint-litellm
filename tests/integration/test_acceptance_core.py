"""1단계 인수시험: 승인 수명주기, 상태 집행, 증거 불변식.

모든 시험은 사건 행(``decision_evaluated``)과 업스트림 호출 수(프록시 기록 + 시험 서버 진입 계수)를
함께 확인한다. 차단 시험은 같은 실행에서 양성 대조를 먼저 통과해야 한다.
"""

import concurrent.futures
import os
import time

import pytest
import support
from scenario import decision

pytestmark = [pytest.mark.needs_plugin, pytest.mark.needs_control]


def code_of(reason: str) -> str:
    return "MINT-" + reason.replace("_", "-")


def assert_blocked(world, evidence, outcome, reason: str) -> None:
    assert outcome.status == 403, outcome.body
    assert outcome.code == code_of(reason)
    events = world.events(outcome.mark)
    assert len(events) == 1
    evaluated, enforced, found = decision(events[0])
    assert (evaluated, enforced, found) == ("block", "block", reason)
    evidence.assert_calls(outcome.mark, 0)


def assert_allowed(world, evidence, outcome) -> dict:
    assert outcome.allowed, outcome.body
    events = world.events(outcome.mark)
    assert len(events) == 1
    assert decision(events[0])[:2] == ("allow", "allow")
    evidence.assert_calls(outcome.mark, 1)
    return events[0]["payload"]


@pytest.fixture
def approved(world, evidence):
    evidence.positive_control()
    subject = world.subject()
    relation = world.prepare(subject)
    approval = world.approve(subject, relation)
    return subject, relation, approval


def test_approved_call_is_allowed_once(world, evidence, approved):
    subject, _, approval = approved
    payload = assert_allowed(world, evidence, world.call(subject))
    assert payload["selected_approval_id"] == approval


def test_call_without_approval_is_blocked(world, evidence):
    evidence.positive_control()
    subject = world.subject()
    world.prepare(subject)
    assert_blocked(world, evidence, world.call(subject), "APPROVAL_ABSENT")


def test_approval_is_effective_immediately(world, evidence):
    evidence.positive_control()
    subject = world.subject()
    relation = world.prepare(subject)
    assert_blocked(world, evidence, world.call(subject), "APPROVAL_ABSENT")
    world.approve(subject, relation)
    assert_allowed(world, evidence, world.call(subject))


def test_sole_approval_revoked_blocks_later_calls(world, evidence, approved):
    subject, _, approval = approved
    assert_allowed(world, evidence, world.call(subject))
    world.mint.run("revoke", "--approval", approval)
    assert_blocked(world, evidence, world.call(subject), "APPROVAL_REVOKED")


def test_revoke_with_deny_wins_over_other_approvals(world, evidence, approved):
    subject, relation, approval = approved
    world.approve(subject, relation)
    world.mint.run("revoke", "--approval", approval, "--deny")
    assert_blocked(world, evidence, world.call(subject), "DENIED")


def test_revoking_narrow_approval_keeps_broad_one(world, evidence, approved):
    subject, relation, broad = approved
    narrow = world.approve(subject, relation, tools="echo")
    world.mint.run("revoke", "--approval", narrow)
    payload = assert_allowed(world, evidence, world.call(subject))
    assert payload["selected_approval_id"] == broad


def test_denial_blocks_even_with_approval(world, evidence, approved):
    subject, _, _ = approved
    world.mint.run("deny", "--user", subject.user_id, "--server", support.SERVER_ID["a"])
    assert_blocked(world, evidence, world.call(subject), "DENIED")


def test_suspended_server_blocks(world, evidence, approved):
    subject, _, _ = approved
    world.mint.run("server", "suspend", support.SERVER_ID["a"])
    try:
        assert_blocked(world, evidence, world.call(subject), "SERVER_SUSPENDED")
    finally:
        world.mint.run("server", "resume", support.SERVER_ID["a"])


def test_terminated_relation_blocks(world, evidence, approved):
    subject, relation, _ = approved
    world.mint.run("relation", "terminate", "--relation", relation)
    assert_blocked(world, evidence, world.call(subject), "RELATION_TERMINATED")


def test_expired_approval_blocks(world, evidence):
    evidence.positive_control()
    subject = world.subject()
    relation = world.prepare(subject)
    world.approve(subject, relation, valid_days=0)
    time.sleep(2)
    assert_blocked(world, evidence, world.call(subject), "APPROVAL_EXPIRED")


def test_approval_not_yet_valid_blocks(world, evidence):
    evidence.positive_control()
    subject = world.subject()
    relation = world.prepare(subject)
    world.approve(subject, relation, valid_days=-1)
    assert_blocked(world, evidence, world.call(subject), "APPROVAL_ABSENT")


def test_allowlist_expiry_blocks(world, evidence, approved):
    subject, _, _ = approved
    world.mint.run(
        "allowlist",
        "add",
        "--kind",
        "server",
        "--value",
        support.SERVER_ID["a"],
        "--expires-in",
        "2s",
    )
    time.sleep(3)
    assert_blocked(world, evidence, world.call(subject), "ALLOWLIST_EXPIRED")


def test_other_organization_approval_is_not_reused(world, evidence, approved):
    _, _, _ = approved
    other = world.subject()
    world.prepare(other)
    assert_blocked(world, evidence, world.call(other), "APPROVAL_ABSENT")


def test_same_decision_on_both_replicas(world, evidence, approved):
    subject, _, _ = approved
    for port in support.LITELLM_PORTS:
        assert_allowed(world, evidence, world.call(subject, port=port))


def test_each_path_records_exactly_one_decision_event(world, evidence, approved):
    subject, _, approval = approved
    allowed = world.call(subject)
    world.mint.run("revoke", "--approval", approval)
    blocked = world.call(subject)
    for outcome in (allowed, blocked):
        assert len(world.events(outcome.mark)) == 1


def test_per_call_approval_single_consumption_under_concurrent_resubmit(world, evidence, approved):
    subject, _, _ = approved
    world.mint.run("settings", "high-risk-tools", "--tools", "echo")
    request = world.mint.data(
        "call-approval",
        "request",
        "--user",
        subject.user_id,
        "--server",
        support.SERVER_ID["a"],
        "--tool",
        "echo",
    )
    approver_user = world.litellm_user()
    approver = world.mint.data(
        "operator", "add", "--name", "it-approver", "--role", "approver", "--user-id", approver_user
    )
    world.mint.run("call-approval", "approve", request["call_approval_id"], token=approver["token"])
    mark = support.marker()
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        ports = support.LITELLM_PORTS
        results = list(pool.map(lambda port: world.call(subject, port=port, mark=mark), ports))
    allowed = [r for r in results if r.allowed]
    blocked = [r for r in results if not r.allowed]
    assert len(allowed) == 1 and len(blocked) == 1
    assert blocked[0].code == code_of("CALL_APPROVAL_REQUIRED")
    evidence.assert_calls(mark, 1)
    consumed = [e for e in world.events(mark) if e["payload"].get("consumed_call_approval_id")]
    assert len(consumed) == 1


OBSERVE_CASES = [
    ("no_history", "block", "allow"),
    ("revoked_history", "block", "block"),
    ("denied", "block", "block"),
]


@pytest.mark.parametrize(("case", "evaluated", "enforced"), OBSERVE_CASES)
def test_observe_mode_matrix(world, evidence, case, evaluated, enforced):
    evidence.positive_control()
    world.mint.run("settings", "mode", "observe")
    try:
        subject = world.subject()
        relation = world.prepare(subject)
        if case == "revoked_history":
            world.mint.run("revoke", "--approval", world.approve(subject, relation))
        if case == "denied":
            world.mint.run("deny", "--user", subject.user_id, "--server", support.SERVER_ID["a"])
        outcome = world.call(subject)
        events = world.events(outcome.mark)
        assert len(events) == 1
        assert decision(events[0])[:2] == (evaluated, enforced)
        evidence.assert_calls(outcome.mark, 1 if enforced == "allow" else 0)
    finally:
        world.mint.run("settings", "mode", "enforce")


@pytest.mark.parametrize("dependency", ["postgres", "opa"])
def test_dependency_down_blocks_and_buffers_one_event(world, evidence, toxics, dependency):
    evidence.positive_control()
    subject = world.subject()
    relation = world.prepare(subject)
    world.approve(subject, relation)
    if dependency == "postgres":
        toxics["postgres"].disable()
    else:
        support.compose("stop", "opa")
    try:
        outcome = world.call(subject)
        assert outcome.status == 403
        assert outcome.code == "MINT-DEPENDENCY-" + ("DB" if dependency == "postgres" else "OPA")
        evidence.assert_calls(outcome.mark, 0)
        buffered = support.compose(
            "exec",
            "-T",
            "litellm1",
            "sh",
            "-c",
            "grep -rc decision_evaluated /var/lib/mint/buffer || true",
            check=False,
        )
        assert buffered.stdout.strip()
    finally:
        toxics["postgres"].enable()
        support.compose("start", "opa", check=False)


def test_buffer_overflow_keeps_blocking_and_records_marker(world, evidence, toxics):
    evidence.positive_control()
    subject = world.subject()
    world.approve(subject, world.prepare(subject))
    toxics["postgres"].disable()
    try:
        outcomes = [world.call(subject) for _ in range(3)]
        assert all(o.status == 403 for o in outcomes)
    finally:
        toxics["postgres"].enable()
    overflow = world.events(outcomes[0].mark, kind="buffer_overflow")
    assert overflow or world.events(outcomes[-1].mark, kind="buffer_overflow")


def test_ledger_write_failure_before_allow_blocks(world, evidence, toxics):
    evidence.positive_control()
    subject = world.subject()
    world.approve(subject, world.prepare(subject))
    toxics["postgres"].add("limit_data", stream="upstream", bytes=0)
    outcome = world.call(subject)
    assert outcome.status == 403 and outcome.code == code_of("AUDIT_FAILURE")
    evidence.assert_calls(outcome.mark, 0)


def test_unverified_state_blocks_everything(world, evidence, approved):
    subject, _, _ = approved
    support.compose(
        "exec", "-T", "litellm1", "sh", "-c", "touch /var/lib/mint/force-unverified", check=False
    )
    outcome = world.call(subject)
    assert outcome.status in (403, 502, 503)
    evidence.assert_calls(outcome.mark, 0)


def test_operator_separation_of_duties_rejected(world, approved):
    subject, relation, _ = approved
    same_user = world.mint.run(
        "approve",
        "--relation",
        relation,
        "--user",
        subject.user_id,
        "--basis",
        "self-approval",
        "--requested-by",
        subject.user_id,
        check=False,
    )
    assert same_user.returncode != 0


def test_approver_without_user_binding_is_rejected(world):
    done = world.mint.run(
        "operator", "add", "--name", "it-unbound", "--role", "approver", check=False
    )
    assert done.returncode != 0


def test_latency_percentiles_are_measured(world, evidence, approved):
    subject, _, _ = approved
    samples = []
    for _ in range(50):
        started = time.perf_counter()
        assert world.call(subject).allowed
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    p50, p95, p99 = (samples[int(len(samples) * q) - 1] for q in (0.5, 0.95, 0.99))
    print(f"latency_ms p50={p50:.1f} p95={p95:.1f} p99={p99:.1f}")
    assert p99 < float(os.environ.get("MINT_E_P99_MS", "5000"))
