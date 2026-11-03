import re
from pathlib import Path

import pytest
from pydantic import ValidationError

import mint_litellm
from mint_litellm.types.decision import (
    SERVER_STATUSES,
    UNSUPPORTED_STAGE,
    Action,
    Conclusion,
    PolicyDecision,
    Stage,
    StageEntry,
    StageResult,
    StageStatus,
    apply_record_failure,
    conclude,
    entries_from_results,
    relation_state_result,
    server_state_result,
    strongest_decision,
)
from mint_litellm.types.reason_codes import Reason
from mint_litellm.types.settings import Mode, ObservationSettings, effective_mode

MIGRATION = Path(mint_litellm.__file__).parent / "control" / "db" / "migrations" / "001_initial.sql"
SQL_SERVERS = MIGRATION.read_text(encoding="utf-8").split("CREATE TABLE mint.servers")[1]

ALL = ObservationSettings()
FINDING = StageResult.finding(Reason.APPROVAL_ABSENT)
STOP = StageResult.stop(Reason.DENIED)


@pytest.mark.parametrize(
    ("mode", "with_finding", "with_stop", "evaluated", "enforced"),
    [
        (Mode.ENFORCE, False, False, Action.ALLOW, Action.ALLOW),
        (Mode.OBSERVE, False, False, Action.ALLOW, Action.ALLOW),
        (Mode.ENFORCE, True, False, Action.BLOCK, Action.BLOCK),
        (Mode.OBSERVE, True, False, Action.BLOCK, Action.ALLOW),
        (Mode.ENFORCE, False, True, Action.BLOCK, Action.BLOCK),
        (Mode.OBSERVE, False, True, Action.BLOCK, Action.BLOCK),
        (Mode.ENFORCE, True, True, Action.BLOCK, Action.BLOCK),
        (Mode.OBSERVE, True, True, Action.BLOCK, Action.BLOCK),
    ],
)
def test_terminator_matrix(mode, with_finding, with_stop, evaluated, enforced):
    results = [StageResult.passed()]
    if with_finding:
        results.append(FINDING)
    if with_stop:
        results.append(STOP)
    out = conclude(results, mode=mode, observation=ALL)
    assert out.evaluated_decision is evaluated
    assert out.enforced_action is enforced
    assert (out.stop is not None) == with_stop
    assert len(out.findings) == int(with_finding)


def test_finding_whose_family_is_not_selected_is_blocked_in_observe():
    only_unregistered = ObservationSettings(
        observable_reasons=frozenset({Reason.SERVER_UNREGISTERED})
    )
    out = conclude([FINDING], mode=Mode.OBSERVE, observation=only_unregistered)
    assert out.enforced_action is Action.BLOCK
    out = conclude(
        [StageResult.finding(Reason.SERVER_UNREGISTERED)],
        mode=Mode.OBSERVE,
        observation=only_unregistered,
    )
    assert out.enforced_action is Action.ALLOW


def test_one_unconverted_finding_blocks_and_all_findings_are_kept():
    only_absent = ObservationSettings(observable_reasons=frozenset({Reason.APPROVAL_ABSENT}))
    second = StageResult.finding(Reason.TOOL_NOT_IN_APPROVAL)
    out = conclude([FINDING, second], mode=Mode.OBSERVE, observation=only_absent)
    assert out.enforced_action is Action.BLOCK
    assert out.findings == (FINDING, second)


def test_default_observation_settings_cover_every_candidate_family():
    from mint_litellm.types.reason_codes import OBSERVATION_CANDIDATES

    assert ObservationSettings().observable_reasons == OBSERVATION_CANDIDATES


@pytest.mark.parametrize(
    ("policy_observable", "listed", "mode", "enforced"),
    [
        (True, True, Mode.OBSERVE, Action.ALLOW),
        (True, False, Mode.OBSERVE, Action.BLOCK),
        (False, True, Mode.OBSERVE, Action.BLOCK),
        (False, False, Mode.OBSERVE, Action.BLOCK),
        (True, True, Mode.ENFORCE, Action.BLOCK),
    ],
)
def test_policy_finding_converts_only_when_declared_and_selected(
    policy_observable, listed, mode, enforced
):
    observation = ObservationSettings(
        observable_policy_ids=frozenset({"POL-E-07"}) if listed else frozenset()
    )
    finding = StageResult.finding(Reason.POLICY_DETECT, "POL-E-07")
    out = conclude(
        [finding], mode=mode, observation=observation, policy_observable=policy_observable
    )
    assert out.enforced_action is enforced
    assert out.evaluated_decision is Action.BLOCK


def test_policy_finding_for_another_policy_id_is_not_converted():
    observation = ObservationSettings(observable_policy_ids=frozenset({"POL-E-07"}))
    finding = StageResult.finding(Reason.POLICY_DETECT, "POL-E-08")
    out = conclude([finding], mode=Mode.OBSERVE, observation=observation, policy_observable=True)
    assert out.enforced_action is Action.BLOCK


def test_a_stop_is_never_converted_even_with_everything_selected():
    out = conclude(
        [FINDING, StageResult.stop(Reason.APPROVAL_REVOKED)],
        mode=Mode.OBSERVE,
        observation=ALL,
        policy_observable=True,
    )
    assert out.enforced_action is Action.BLOCK
    assert out.stop is not None


def test_two_stops_are_a_pipeline_bug():
    with pytest.raises(ValueError):
        conclude([STOP, STOP], mode=Mode.ENFORCE, observation=ALL)


def test_finding_and_stop_reasons_are_kept_apart():
    with pytest.raises(ValidationError):
        StageResult(status=StageStatus.FINDING, reason_code="DENIED")
    with pytest.raises(ValidationError):
        StageResult(status=StageStatus.STOP, reason_code="APPROVAL_ABSENT")
    with pytest.raises(ValidationError):
        StageResult(status=StageStatus.STOP)
    with pytest.raises(ValidationError):
        StageResult(status=StageStatus.PASS, reason_code="DENIED")
    with pytest.raises(ValidationError):
        StageResult(status=StageStatus.SKIPPED)
    with pytest.raises(ValidationError):
        StageResult(status=StageStatus.FINDING, reason_code="UNKNOWN")


def test_record_failure_blocks_an_allowed_call():
    allowed = conclude([], mode=Mode.ENFORCE, observation=ALL)
    out = apply_record_failure(allowed)
    assert out.enforced_action is Action.BLOCK
    assert out.evaluated_decision is Action.BLOCK
    assert out.stop is not None and out.stop.reason_code == "AUDIT_FAILURE"


def test_record_failure_on_a_converted_finding_blocks_and_keeps_the_findings():
    converted = conclude([FINDING], mode=Mode.OBSERVE, observation=ALL)
    assert converted.enforced_action is Action.ALLOW
    out = apply_record_failure(converted)
    assert out.enforced_action is Action.BLOCK
    assert out.findings == (FINDING,)
    assert out.stop is not None and out.stop.reason_code == "AUDIT_FAILURE"


def test_record_failure_keeps_the_original_reason_on_a_blocked_call():
    blocked = conclude([STOP], mode=Mode.ENFORCE, observation=ALL)
    assert apply_record_failure(blocked) == blocked


def test_stop_skips_the_remaining_stages():
    entries = entries_from_results(
        [
            (Stage.CONTEXT, StageResult.passed()),
            (Stage.SNAPSHOT, StageResult.passed()),
            (Stage.STATE, StageResult.stop(Reason.SERVER_SUSPENDED)),
        ]
    )
    by_stage = {e.stage: e for e in entries}
    assert [e.stage for e in entries] == list(Stage)
    assert by_stage[Stage.STATE].status is StageStatus.STOP
    for stage in (Stage.DENIAL_APPROVAL, Stage.CONTRACT, Stage.POLICY, Stage.OBLIGATIONS):
        assert by_stage[stage].status is StageStatus.SKIPPED
        assert by_stage[stage].missing_reason == "after_stop"


def test_policy_decision_priority():
    d = PolicyDecision
    assert strongest_decision([d.ALLOW, d.ALERT]) is d.ALERT
    assert strongest_decision([d.ALERT, d.REQUIRE_APPROVAL]) is d.REQUIRE_APPROVAL
    assert strongest_decision([d.REQUIRE_APPROVAL, d.BLOCK, d.ALLOW]) is d.BLOCK
    assert strongest_decision([]) is d.ALLOW


@pytest.mark.parametrize(
    ("ceiling", "stored", "expected"),
    [
        (Mode.ENFORCE, Mode.ENFORCE, Mode.ENFORCE),
        (Mode.ENFORCE, Mode.OBSERVE, Mode.ENFORCE),
        (Mode.OBSERVE, Mode.ENFORCE, Mode.ENFORCE),
        (Mode.OBSERVE, Mode.OBSERVE, Mode.OBSERVE),
    ],
)
def test_environment_ceiling_wins_over_stored_mode(ceiling, stored, expected):
    assert effective_mode(ceiling, stored) is expected


def test_observation_settings_reject_reasons_that_cannot_convert():
    with pytest.raises(ValidationError):
        ObservationSettings(observable_reasons=frozenset({Reason.DENIED}))


@pytest.mark.parametrize(
    ("status", "kind", "reason"),
    [
        (None, StageStatus.FINDING, "SERVER_UNREGISTERED"),
        ("pending", StageStatus.FINDING, "SERVER_NOT_APPROVED"),
        ("validating", StageStatus.FINDING, "SERVER_NOT_APPROVED"),
        ("awaiting_approval", StageStatus.FINDING, "SERVER_NOT_APPROVED"),
        ("rejected", StageStatus.STOP, "SERVER_REJECTED"),
        ("approved", StageStatus.PASS, None),
        ("suspended", StageStatus.STOP, "SERVER_SUSPENDED"),
        ("terminating", StageStatus.STOP, "SERVER_TERMINATED"),
        ("terminated", StageStatus.STOP, "SERVER_TERMINATED"),
    ],
)
def test_server_state_enforcement_table(status, kind, reason):
    result = server_state_result(status)
    assert (result.status, result.reason_code) == (kind, reason)


def test_server_state_table_covers_every_stored_status():
    listed = re.search(r"CHECK \(status IN \((.*?)\)\)", SQL_SERVERS, re.S).group(1)
    assert set(re.findall(r"'([a-z_]+)'", listed)) == SERVER_STATUSES


def test_relation_state_enforcement():
    assert relation_state_result("active").status is StageStatus.PASS
    for status in ("terminating", "terminated"):
        result = relation_state_result(status)
        assert (result.status, result.reason_code) == (StageStatus.STOP, "RELATION_TERMINATED")
    with pytest.raises(ValueError):
        relation_state_result("paused")
    with pytest.raises(ValueError):
        server_state_result("paused")


def test_conclusion_rejects_misplaced_results_and_inconsistent_outcomes():
    finding = StageResult.finding(Reason.APPROVAL_ABSENT)
    stop = StageResult.stop(Reason.DENIED)
    with pytest.raises(ValidationError):
        Conclusion(evaluated_decision=Action.BLOCK, enforced_action=Action.ALLOW, findings=(stop,))
    with pytest.raises(ValidationError):
        Conclusion(
            evaluated_decision=Action.BLOCK, enforced_action=Action.BLOCK, stop=StageResult.passed()
        )
    with pytest.raises(ValidationError):
        Conclusion(
            evaluated_decision=Action.ALLOW, enforced_action=Action.ALLOW, findings=(finding,)
        )
    with pytest.raises(ValidationError):
        Conclusion(evaluated_decision=Action.BLOCK, enforced_action=Action.ALLOW, stop=stop)
    Conclusion(evaluated_decision=Action.BLOCK, enforced_action=Action.ALLOW, findings=(finding,))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": StageStatus.PASS, "reason_code": "DENIED"},
        {"status": StageStatus.SKIPPED, "reason_code": "DENIED", "missing_reason": "x"},
        {"status": StageStatus.FINDING},
        {"status": StageStatus.FINDING, "reason_code": "DENIED"},
        {"status": StageStatus.STOP, "reason_code": "APPROVAL_ABSENT"},
        {"status": StageStatus.STOP},
        {"status": StageStatus.PASS, "missing_reason": "not_reached"},
        {"status": StageStatus.SKIPPED},
    ],
)
def test_stage_entry_rejects_inconsistent_status_and_reason(kwargs):
    with pytest.raises(ValidationError):
        StageEntry(stage=Stage.POLICY, **kwargs)


def test_stage_entry_accepts_the_legal_shapes():
    StageEntry(stage=Stage.POLICY, status=StageStatus.PASS)
    StageEntry(stage=Stage.POLICY, status=StageStatus.FINDING, reason_code="APPROVAL_ABSENT")
    StageEntry(stage=Stage.POLICY, status=StageStatus.STOP, reason_code="DENIED")
    StageEntry(stage=Stage.CONTRACT, status=StageStatus.SKIPPED, missing_reason="after_stop")


def test_unsupported_contract_stage_is_recorded_without_stopping_the_pipeline():
    entries = entries_from_results(
        [
            (Stage.CONTEXT, StageResult.passed()),
            (Stage.SNAPSHOT, StageResult.passed()),
            (Stage.STATE, StageResult.passed()),
            (Stage.DENIAL_APPROVAL, StageResult.finding(Reason.APPROVAL_ABSENT)),
            (Stage.CONTRACT, UNSUPPORTED_STAGE),
            (Stage.POLICY, StageResult.passed()),
            (Stage.OBLIGATIONS, StageResult.passed()),
        ]
    )
    by_stage = {e.stage: e for e in entries}
    assert [e.stage for e in entries] == list(Stage)
    assert by_stage[Stage.CONTRACT].status is StageStatus.SKIPPED
    assert by_stage[Stage.CONTRACT].missing_reason == "unsupported_stage"
    assert by_stage[Stage.POLICY].status is StageStatus.PASS
    assert by_stage[Stage.DENIAL_APPROVAL].status is StageStatus.FINDING


def test_explicit_skip_and_stop_together_do_not_duplicate_stages():
    entries = entries_from_results(
        [
            (Stage.CONTEXT, StageResult.passed()),
            (Stage.CONTRACT, UNSUPPORTED_STAGE),
            (Stage.STATE, StageResult.stop(Reason.SERVER_SUSPENDED)),
        ]
    )
    stages = [e.stage for e in entries]
    assert len(stages) == len(set(stages))
    by_stage = {e.stage: e for e in entries}
    assert by_stage[Stage.CONTRACT].missing_reason == "unsupported_stage"
    assert by_stage[Stage.POLICY].missing_reason == "after_stop"
