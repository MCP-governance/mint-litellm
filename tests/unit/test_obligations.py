import pytest

from mint_litellm.plugin.obligations import (
    SUPPORTED_OBLIGATIONS,
    AlertObligation,
    RequireCallApproval,
    RequireConsent,
    compose_obligations,
    normalize_policy_output,
)
from mint_litellm.types.decision import (
    Action,
    ObligationType,
    PolicyDecision,
    Severity,
    conclude,
)
from mint_litellm.types.errors import error_for_reason
from mint_litellm.types.settings import Mode, ObservationSettings


def test_supported_set_is_the_three_obligations():
    assert {o.value for o in SUPPORTED_OBLIGATIONS} == {
        "alert",
        "require_call_approval",
        "require_consent",
    }


def test_alerts_compose_to_the_highest_severity():
    out = compose_obligations(
        [
            {"type": "alert", "severity": "low"},
            {"type": "alert", "severity": "critical"},
            {"type": "alert", "severity": "medium"},
        ]
    )
    assert out.failure is None
    assert out.obligations == (AlertObligation(severity=Severity.CRITICAL),)


def test_duplicates_collapse_and_order_is_fixed():
    out = compose_obligations(
        [
            {"type": "require_consent", "purpose": "analytics"},
            {"type": "require_call_approval"},
            {"type": "require_consent", "purpose": "analytics"},
            {"type": "require_call_approval"},
            {"type": "alert", "severity": "high"},
        ]
    )
    assert out.obligations == (
        AlertObligation(severity=Severity.HIGH),
        RequireCallApproval(),
        RequireConsent(purpose="analytics"),
    )
    assert out.obligation_types == (
        ObligationType.ALERT,
        ObligationType.REQUIRE_CALL_APPROVAL,
        ObligationType.REQUIRE_CONSENT,
    )


def test_conflicting_consent_purposes_stop_the_call():
    out = compose_obligations(
        [
            {"type": "require_consent", "purpose": "analytics"},
            {"type": "require_consent", "purpose": "training"},
        ]
    )
    assert out.failure is not None
    assert out.failure.reason_code == "UNSUPPORTED"
    assert out.obligations == ()


@pytest.mark.parametrize("kind", ["redact_keys", "cap_size", "set_value", "Alert", ""])
def test_unsupported_obligation_stops_the_call_with_the_unsupported_error(kind):
    out = compose_obligations([{"type": kind}])
    assert out.failure is not None and out.failure.reason_code == "UNSUPPORTED"
    assert error_for_reason(out.failure.reason_code, "ref").code == "MINT-UNSUPPORTED"


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"type": 3},
        {"type": "alert"},
        {"type": "alert", "severity": "urgent"},
        {"type": "alert", "severity": "high", "extra": 1},
        {"type": "require_consent"},
        {"type": "require_consent", "purpose": "  "},
        {"type": "require_call_approval", "extra": 1},
    ],
)
def test_malformed_obligations_are_schema_errors(item):
    out = compose_obligations([item])
    assert out.failure is not None
    assert out.failure.reason_code in {"SCHEMA_ERROR", "UNSUPPORTED"}
    if "type" in item and item["type"] in {t.value for t in ObligationType}:
        assert out.failure.reason_code == "SCHEMA_ERROR"


def test_first_failure_wins_in_input_order():
    out = compose_obligations([{"type": "alert", "severity": "bad"}, {"type": "cap_size"}])
    assert out.failure is not None and out.failure.reason_code == "SCHEMA_ERROR"


def test_require_approval_becomes_a_call_approval_obligation():
    out = normalize_policy_output("RequireApproval", ["POL-R-01"], [])
    assert out.decision is PolicyDecision.REQUIRE_APPROVAL
    assert out.obligations == (RequireCallApproval(),)
    assert out.findings == ()


def test_require_approval_merges_with_an_explicit_one():
    out = normalize_policy_output(
        "RequireApproval",
        [],
        [{"type": "require_call_approval"}, {"type": "alert", "severity": "low"}],
    )
    assert out.obligation_types == (ObligationType.ALERT, ObligationType.REQUIRE_CALL_APPROVAL)


def test_block_becomes_one_finding_per_policy():
    out = normalize_policy_output("Block", ["POL-E-07", "POL-E-08"], [])
    assert [f.reason_code for f in out.findings] == [
        "POLICY_DETECT_POL-E-07",
        "POLICY_DETECT_POL-E-08",
    ]
    assert out.failure is None


@pytest.mark.parametrize("decision", ["Deny", "block", "", None, 1])
def test_unknown_decision_values_are_schema_errors(decision):
    out = normalize_policy_output(decision, [], [])
    assert out.failure is not None and out.failure.reason_code == "SCHEMA_ERROR"


def test_block_needs_valid_policy_ids():
    for ids in ([], ["has space"]):
        out = normalize_policy_output("Block", ids, [])
        assert out.failure is not None and out.failure.reason_code == "SCHEMA_ERROR"


def test_unsupported_obligation_fails_even_for_an_allow_decision():
    out = normalize_policy_output("Allow", [], [{"type": "redact_keys"}])
    assert out.failure is not None and out.failure.reason_code == "UNSUPPORTED"


def test_normalized_block_converts_in_observe_only_when_declared_and_selected():
    out = normalize_policy_output("Block", ["POL-E-07"], [])
    observation = ObservationSettings(observable_policy_ids=frozenset({"POL-E-07"}))
    allowed = conclude(
        out.findings, mode=Mode.OBSERVE, observation=observation, policy_observable=True
    )
    assert allowed.enforced_action is Action.ALLOW
    blocked = conclude(
        out.findings, mode=Mode.OBSERVE, observation=observation, policy_observable=False
    )
    assert blocked.enforced_action is Action.BLOCK


def test_the_failure_stops_the_pipeline():
    out = normalize_policy_output("Allow", [], [{"type": "redact_keys"}])
    result = conclude([out.failure], mode=Mode.OBSERVE, observation=ObservationSettings())
    assert result.enforced_action is Action.BLOCK
