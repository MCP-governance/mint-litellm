from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from mint_litellm.types.decision import (
    MINT_CTX_KEY,
    MINT_DECISION_REF_KEY,
    MISSING_REASON_FIELDS,
    Action,
    DecisionSnapshot,
    MintContext,
    ObligationType,
    OperationType,
    PolicyDecision,
    ResolvedServer,
    StageResult,
)
from mint_litellm.types.events import EventKind, decision_event
from mint_litellm.types.identity import Principal, PrincipalType, Provenance
from mint_litellm.types.reason_codes import Reason
from mint_litellm.types.settings import Mode

INV = UUID("0192f7a0-7c1e-7a10-8000-0123456789ab")
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
PRINCIPAL = Principal(
    instance_id="inst",
    principal_type=PrincipalType.USER,
    principal_id="u1",
    key_id="k" * 64,
    team_id="t1",
    org_id="org1",
    org_source="team",
    provenance={"org_id": Provenance.DERIVED},
)


def snapshot(**override):
    ctx = MintContext(invocation_id=INV, attempt_id=1)
    base = {
        "invocation_id": INV,
        "attempt_id": 1,
        "decision_ref": ctx.decision_ref,
        "litellm_call_id": "call-1",
        "operation": OperationType.TOOL_CALL,
        "evaluated_at": NOW,
        "principal": PRINCIPAL,
        "requested_server_name": "github",
        "tool": "create_issue",
        "resolved_server_id": "srv-1",
        "resolved_url_sha256": "a" * 64,
        "resolved_transport": "http",
        "resolved_auth_type": "bearer_token",
        "candidates_with_same_name": 1,
        "mode": Mode.ENFORCE,
        "state_epoch": 7,
        "settings_version": 2,
        "policy_version_id": "pv-1",
        "bundle_sha256": "b" * 64,
        "policy_decision": PolicyDecision.ALLOW,
        "evaluated_decision": Action.ALLOW,
        "enforced_action": Action.ALLOW,
        "selected_approval_id": "ap-1",
        "consumed_call_approval_id": None,
        "revoked_after_snapshot": None,
        "revocation_check": None,
        "missing_reasons": {
            "consumed_call_approval_id": "not_applicable",
            "revoked_after_snapshot": "pending_response",
            "revocation_check": "pending_response",
        },
    }
    return DecisionSnapshot(**{**base, **override})


def test_valid_snapshot_exposes_the_contract_fields():
    dumped = snapshot().model_dump(mode="json")
    for field in (
        "decision_ref",
        "resolved_server_id",
        "resolved_url_sha256",
        "candidates_with_same_name",
        "revoked_after_snapshot",
        "state_epoch",
        "evaluated_decision",
        "enforced_action",
        "obligation_types",
        "findings",
        "stages",
    ):
        assert field in dumped
    assert dumped["decision_ref"] == f"{INV}:1"


def test_context_keys_are_the_documented_slots():
    assert MINT_CTX_KEY == "mint_ctx"
    assert MINT_DECISION_REF_KEY == "mint_decision_ref"


def test_decision_ref_must_match_the_invocation():
    with pytest.raises(ValidationError):
        snapshot(decision_ref="someone-else")


def test_null_without_a_missing_reason_is_rejected():
    reasons = dict(snapshot().missing_reasons)
    del reasons["revoked_after_snapshot"]
    with pytest.raises(ValidationError):
        snapshot(missing_reasons=reasons)


def test_value_with_a_missing_reason_is_rejected():
    with pytest.raises(ValidationError):
        snapshot(missing_reasons={**snapshot().missing_reasons, "state_epoch": "not_reached"})


def test_missing_reason_for_unknown_field_or_bad_format_is_rejected():
    with pytest.raises(ValidationError):
        snapshot(missing_reasons={**snapshot().missing_reasons, "nonsense": "x"})
    with pytest.raises(ValidationError):
        snapshot(
            consumed_call_approval_id=None,
            missing_reasons={**snapshot().missing_reasons, "consumed_call_approval_id": "Not Ok"},
        )


def test_every_nullable_field_is_listed_for_missing_reason_checks():
    assert set(MISSING_REASON_FIELDS) <= set(DecisionSnapshot.model_fields)


def test_evaluated_and_enforced_must_agree_with_findings_and_stop():
    finding = StageResult.finding(Reason.APPROVAL_ABSENT)
    stop = StageResult.stop(Reason.DENIED)
    with pytest.raises(ValidationError):
        snapshot(findings=(finding,))
    with pytest.raises(ValidationError):
        snapshot(findings=(finding,), evaluated_decision=Action.BLOCK)
    with pytest.raises(ValidationError):
        snapshot(stop=stop, evaluated_decision=Action.BLOCK, enforced_action=Action.ALLOW)
    snapshot(findings=(finding,), evaluated_decision=Action.BLOCK, enforced_action=Action.BLOCK)
    snapshot(
        mode=Mode.OBSERVE,
        findings=(finding,),
        evaluated_decision=Action.BLOCK,
        enforced_action=Action.ALLOW,
    )
    snapshot(stop=stop, evaluated_decision=Action.BLOCK, enforced_action=Action.BLOCK)


def test_snapshot_carries_obligation_types_not_values():
    snap = snapshot(obligation_types=(ObligationType.ALERT, ObligationType.REQUIRE_CONSENT))
    assert snap.model_dump(mode="json")["obligation_types"] == ["alert", "require_consent"]


def test_resolved_server_requires_a_candidate_count():
    ResolvedServer(
        server_id="s",
        url_sha256="a" * 64,
        transport="http",
        auth_type="none",
        candidates_with_same_name=2,
    )
    with pytest.raises(ValidationError):
        ResolvedServer(
            server_id="s",
            url_sha256="a" * 64,
            transport="http",
            auth_type="none",
            candidates_with_same_name=0,
        )


def test_decision_event_for_a_tool_call():
    event = decision_event(snapshot())
    assert event.kind is EventKind.DECISION_EVALUATED
    assert event.source_key == f"call:{INV}:1:decision_evaluated"
    assert event.invocation_id == INV and event.attempt_id == 1
    assert event.subject == {"principal_type": "user", "principal_id": "u1", "org_id": "org1"}
    assert event.payload["enforced_action"] == "allow"


def test_decision_event_for_discovery_is_per_tool_with_attempt_one():
    snap = snapshot(operation=OperationType.RUNTIME_DISCOVERY)
    event = decision_event(snap)
    assert event.kind is EventKind.DISCOVERY_EVALUATED
    assert event.source_key == f"call:{INV}:1:discovery_evaluated"
    ctx = MintContext(invocation_id=INV, attempt_id=2)
    with pytest.raises(ValueError):
        decision_event(
            snapshot(
                operation=OperationType.RUNTIME_DISCOVERY,
                attempt_id=2,
                decision_ref=ctx.decision_ref,
            )
        )


def test_a_stop_placed_in_the_findings_slot_is_rejected():
    stop = StageResult.stop(Reason.DENIED)
    with pytest.raises(ValidationError):
        snapshot(
            mode=Mode.OBSERVE,
            findings=(stop,),
            stop=None,
            evaluated_decision=Action.BLOCK,
            enforced_action=Action.ALLOW,
        )


def test_a_pass_placed_in_the_stop_slot_is_rejected():
    with pytest.raises(ValidationError):
        snapshot(
            stop=StageResult.passed(),
            evaluated_decision=Action.BLOCK,
            enforced_action=Action.BLOCK,
        )
    with pytest.raises(ValidationError):
        snapshot(
            findings=(StageResult.passed(),),
            evaluated_decision=Action.BLOCK,
            enforced_action=Action.BLOCK,
        )
    with pytest.raises(ValidationError):
        snapshot(
            findings=(StageResult.finding(Reason.APPROVAL_ABSENT),),
            stop=StageResult.finding(Reason.APPROVAL_ABSENT),
            evaluated_decision=Action.BLOCK,
            enforced_action=Action.BLOCK,
        )
