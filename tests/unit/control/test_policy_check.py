"""활성 정책 판본의 내용 대조(check·status). 정책 엔진 응답은 OPA 1.21 형식의 고정 응답이다."""


import pytest
from ctl_fixtures import INSTANCE

pytest.importorskip("psycopg")

from fake_opa import DATA, RULES, FakeOpa, expected_hash  # noqa: E402

from mint_litellm.control import checks, policy_check  # noqa: E402
from mint_litellm.control.checks import Level  # noqa: E402


@pytest.fixture
def opa():
    server = FakeOpa()
    yield server
    server.close()


def activate(conn, digest):
    conn.execute(
        "INSERT INTO mint.policy_versions (instance_id, id, catalog_sha256, bundle_sha256)"
        " VALUES (%s, 'v1', %s, %s)", (INSTANCE, "a" * 64, digest))
    for status in ("staged", "active"):
        conn.execute("UPDATE mint.policy_versions SET status = %s, activated_at = now(),"
                     " activated_by = 'x' WHERE id = 'v1'", (status,))


def test_matching_content_is_reported_as_a_match(conn, opa):
    activate(conn, expected_hash())
    state = policy_check.verify_active_policy(conn, INSTANCE, opa.url)
    assert (state.state, state.version) == ("match", "v1")


def test_changed_rules_or_data_with_unchanged_registration_are_a_mismatch(conn, opa):
    activate(conn, expected_hash())
    opa.rules = RULES + "# 바뀐 규칙\n"
    assert policy_check.verify_active_policy(conn, INSTANCE, opa.url).state == "mismatch"
    opa.rules = RULES
    opa.data = {**DATA, "business_hours": {**DATA["business_hours"], "end": "23:00"}}
    state = policy_check.verify_active_policy(conn, INSTANCE, opa.url)
    assert state.state == "mismatch" and state.expected != state.loaded


def test_an_unexpected_bundle_layout_is_a_mismatch(conn, opa):
    activate(conn, expected_hash())
    opa.bundles = {"mint": {}, "other": {}}
    assert policy_check.verify_active_policy(conn, INSTANCE, opa.url).state == "mismatch"


def test_an_unreachable_engine_or_missing_address_cannot_be_verified(conn):
    activate(conn, expected_hash())
    assert policy_check.verify_active_policy(conn, INSTANCE, None).state == "unverified"
    down = policy_check.verify_active_policy(conn, INSTANCE, "http://localhost:9")
    assert down.state == "unverified" and "읽지 못했습니다" in down.detail


def test_no_active_version_is_not_a_comparison(conn, opa):
    assert policy_check.verify_active_policy(conn, INSTANCE, opa.url).state == "none"


def run(conn, state):
    config = {"general_settings": {}}
    return checks.run_checks(config, {}, None, compat_probe=lambda: [], policy_state=state)


def levels(report):
    return [f.level for f in report.findings if f.item == checks.POLICY_ITEM]


def test_check_fails_on_a_mismatch_and_reports_unverified_as_incomplete(conn, opa):
    activate(conn, expected_hash())
    assert levels(run(conn, policy_check.verify_active_policy(conn, INSTANCE, opa.url))) == [
        Level.OK]
    opa.rules = "package mint\n"
    mismatch = run(conn, policy_check.verify_active_policy(conn, INSTANCE, opa.url))
    assert levels(mismatch) == [Level.FAIL] and mismatch.failed
    unknown = run(conn, policy_check.verify_active_policy(conn, INSTANCE, None))
    assert levels(unknown) == [Level.SKIP] and checks.POLICY_ITEM in unknown.incomplete
    none = run(conn, policy_check.PolicyState("none", detail="활성 정책 판본이 없습니다."))
    assert levels(none) == [Level.WARN] and checks.POLICY_ITEM not in none.incomplete
