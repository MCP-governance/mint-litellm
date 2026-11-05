import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from plugin_helpers import (
    BUNDLE,
    FP_KEY,
    NOW,
    allowlist,
    approval,
    buffered,
    call_approval,
    config,
    consent,
    denial,
    evaluator,
    harness,
    request,
    resolved_server,
    server_row,
    snapshot,
    user_principal,
)

from mint_litellm.compat.litellm_1_106 import HookData, ServerResolutionError
from mint_litellm.plugin.pipeline import (
    Deadline,
    blocking_reason,
    build_policy_input,
    build_request,
    evaluate_request,
)
from mint_litellm.plugin.policy.input import has_only_policy_fields
from mint_litellm.plugin.snapshot import RelationRow, SettingsRow
from mint_litellm.types.decision import (
    Action,
    OperationType,
    Stage,
    StageResult,
    StageStatus,
    url_sha256,
)
from mint_litellm.types.errors import DispatchState
from mint_litellm.types.fingerprint import argument_fingerprint
from mint_litellm.types.identity import OrgStatus, SubjectType
from mint_litellm.types.reason_codes import OBSERVATION_CANDIDATES, Reason
from mint_litellm.types.settings import Mode, ObservationSettings


def run(coro):
    return asyncio.run(coro)


def decide(h, req=None):
    return run(evaluate_request(req or request(), h.deps))


def code(outcome) -> str:
    assert outcome.error is not None, "expected a block"
    return outcome.error.code


def test_allow_records_exactly_one_decision_event_in_the_database(tmp_path):
    h = harness(tmp_path)
    outcome = decide(h)
    assert outcome.allowed and outcome.error is None
    assert outcome.evidence.value == "db"
    events = h.store.of_kind("decision_evaluated")
    assert len(events) == 1
    snap = outcome.snapshot
    assert snap.selected_approval_id == "00000000-0000-0000-0000-0000000000a1"
    assert snap.state_epoch == 7 and snap.settings_version == 3
    assert snap.policy_version_id == "pv1" and snap.bundle_sha256 == BUNDLE
    assert snap.principal.org_id == "org1" and snap.principal.org_source.value == "team"
    assert not h.buffer.has_pending() and h.stderr.getvalue() == ""


def test_context_document_carries_only_plain_values(tmp_path):
    outcome = decide(harness(tmp_path))
    ctx = outcome.context
    assert json.loads(json.dumps(ctx)) == ctx
    assert ctx["allowed"] is True and ctx["state_epoch"] == 7
    assert ctx["server_id"] == "srv-1" and ctx["tool"] == "search"
    assert ctx["approval_id"] == "00000000-0000-0000-0000-0000000000a1"
    assert ctx["relation_id"] == "r1" and ctx["org_id"] == "org1"
    assert "q" not in json.dumps(ctx)


class TestContextStage:
    def test_unverified_blocks_without_reading_the_snapshot(self, tmp_path):
        h = harness(tmp_path, verified=False)
        outcome = decide(h)
        assert code(outcome) == "MINT-ENFORCEMENT-UNVERIFIED"
        assert h.snapshots.queries == []
        assert len(h.store.of_kind("decision_evaluated")) == 1
        assert outcome.snapshot.stop.reason_code == "ENFORCEMENT_UNVERIFIED"
        assert outcome.error.dispatch_state is DispatchState.NOT_DISPATCHED
        assert outcome.error.retry_safe is False

    def test_master_key_is_forbidden(self, tmp_path):
        h = harness(tmp_path)
        principal = user_principal(user_id="default_user_id", team_id=None, is_master_key=True)
        outcome = decide(h, request(principal))
        assert code(outcome) == "MINT-MASTER-KEY-FORBIDDEN"
        assert h.snapshots.queries == []

    def test_unresolved_server_stops(self, tmp_path):
        h = harness(tmp_path)
        failure = StageResult.stop(Reason.SERVER_UNRESOLVED, detail="ValueError")
        outcome = decide(h, request(resolved=None, failure=failure))
        assert code(outcome) == "MINT-SERVER-UNRESOLVED"
        snap = outcome.snapshot
        assert snap.resolved_server_id is None
        assert snap.missing_reasons["resolved_server_id"] == "server_unresolved"

    def test_two_candidates_with_the_same_name_are_ambiguous(self, tmp_path):
        h = harness(tmp_path)
        outcome = decide(h, request(resolved=resolved_server(candidates_with_same_name=2)))
        assert code(outcome) == "MINT-SERVER-IDENTITY-AMBIGUOUS"
        assert outcome.snapshot.candidates_with_same_name == 2

    def test_unverified_wins_over_master_key(self, tmp_path):
        h = harness(tmp_path, verified=False)
        principal = user_principal(is_master_key=True)
        assert code(decide(h, request(principal))) == "MINT-ENFORCEMENT-UNVERIFIED"


class TestSnapshotFailure:
    def test_database_failure_blocks_with_a_retry_safe_error_and_a_buffer_event(self, tmp_path):
        h = harness(tmp_path, RuntimeError("down"))
        h.store.fail = RuntimeError("down")
        outcome = decide(h)
        assert code(outcome) == "MINT-DEPENDENCY-DB"
        assert outcome.error.retry_safe is True
        assert outcome.evidence.value == "buffer"
        assert h.store.events == {}
        events = buffered(h)
        assert [e.kind.value for e in events] == ["decision_evaluated"]
        assert events[0].evidence.value == "buffer"

    def test_database_and_buffer_failure_writes_one_structured_stderr_line(self, tmp_path):
        h = harness(tmp_path, RuntimeError("down"))
        h.store.fail = RuntimeError("down")
        blocker = tmp_path / "blocked"
        blocker.write_text("file, not a directory")
        h.buffer._dir = blocker / "inner"
        outcome = decide(h)
        assert code(outcome) == "MINT-DEPENDENCY-DB" and outcome.evidence.value == "stderr"
        lines = h.stderr.getvalue().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["evidence"] == "stderr" and record["kind"] == "decision_evaluated"
        assert record["source_key"].startswith("call:")

    def test_snapshot_budget_exceeded_is_a_dependency_failure(self, tmp_path):
        h = harness(tmp_path, cfg=config(tmp_path, decision_budget_seconds=0.05))

        async def slow(query):
            await asyncio.sleep(1)

        h.snapshots.read_snapshot = slow
        assert code(decide(h)) == "MINT-DEPENDENCY-DB"


class TestStateStage:
    def test_anonymous_is_blocked(self, tmp_path):
        h = harness(tmp_path)
        anonymous = user_principal(user_id=None, team_id=None)
        outcome = decide(h, request(anonymous))
        assert code(outcome) == "MINT-ANONYMOUS"

    def test_a_key_without_user_or_team_is_anonymous(self, tmp_path):
        anonymous = user_principal(user_id=None, team_id=None)
        assert anonymous.principal_type.value == "anonymous"

    def test_no_binding_and_no_default_org_is_an_organization_boundary_stop(self, tmp_path):
        h = harness(tmp_path, snapshot(bindings={}))
        assert code(decide(h)) == "MINT-ORG-BOUNDARY"

    def test_a_closed_organization_is_an_organization_boundary_stop(self, tmp_path):
        h = harness(tmp_path, snapshot(org_status={"org1": OrgStatus.CLOSED}))
        assert code(decide(h)) == "MINT-ORG-BOUNDARY"

    def test_default_org_applies_when_no_binding_exists(self, tmp_path):
        snap = snapshot(
            bindings={},
            settings=SettingsRow(default_org_id="org1", settings_version=1),
        )
        outcome = decide(harness(tmp_path, snap))
        assert outcome.allowed
        assert outcome.snapshot.principal.org_source.value == "default"

    def test_user_binding_is_used_when_the_key_has_no_team(self, tmp_path):
        snap = snapshot(
            bindings={(SubjectType.USER, "u1"): "org1"},
            approvals=(approval(scope_type="user", scope_id="u1"),),
        )
        principal = user_principal(team_id=None)
        outcome = decide(harness(tmp_path, snap), request(principal))
        assert outcome.allowed
        assert outcome.snapshot.principal.org_source.value == "user"

    def test_a_team_binding_wins_over_a_user_binding(self, tmp_path):
        snap = snapshot(
            bindings={(SubjectType.TEAM, "t1"): "org1", (SubjectType.USER, "u1"): "org2"},
            org_status={"org1": OrgStatus.ACTIVE, "org2": OrgStatus.ACTIVE},
        )
        outcome = decide(harness(tmp_path, snap))
        assert outcome.snapshot.principal.org_id == "org1"

    def test_only_out_of_organization_approvals_is_a_boundary_stop(self, tmp_path):
        snap = snapshot(approvals=(approval(org_id="org2"),))
        assert code(decide(harness(tmp_path, snap))) == "MINT-ORG-BOUNDARY"

    def test_an_in_organization_approval_beats_an_out_of_organization_one(self, tmp_path):
        snap = snapshot(approvals=(approval(org_id="org2"), approval(id="a2")))
        outcome = decide(harness(tmp_path, snap))
        assert outcome.allowed and outcome.snapshot.selected_approval_id == "a2"

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("rejected", "MINT-SERVER-REJECTED"),
            ("suspended", "MINT-SERVER-SUSPENDED"),
            ("terminating", "MINT-SERVER-TERMINATED"),
            ("terminated", "MINT-SERVER-TERMINATED"),
        ],
    )
    def test_negative_server_states_stop_in_every_mode(self, tmp_path, status, expected):
        snap = snapshot(
            server=server_row(status=status),
            settings=SettingsRow(mode=Mode.OBSERVE, settings_version=1),
        )
        h = harness(tmp_path, snap, cfg=config(tmp_path, mode_ceiling=Mode.OBSERVE))
        assert code(decide(h)) == expected

    @pytest.mark.parametrize("status", ["pending", "validating", "awaiting_approval"])
    def test_unapproved_server_is_a_finding_that_enforce_mode_blocks(self, tmp_path, status):
        h = harness(tmp_path, snapshot(server=server_row(status=status)))
        outcome = decide(h)
        assert code(outcome) == "MINT-SERVER-NOT-APPROVED"
        assert outcome.snapshot.stop is None
        assert outcome.snapshot.findings[0].reason_code == "SERVER_NOT_APPROVED"

    def test_unregistered_server_is_a_finding_and_the_pipeline_continues(self, tmp_path):
        snap = snapshot(server=None, approvals=())
        calls: list = []
        h = harness(tmp_path, snap, evaluate=evaluator(calls=calls))
        outcome = decide(h)
        assert code(outcome) == "MINT-SERVER-UNREGISTERED"
        reasons = [f.reason_code for f in outcome.snapshot.findings]
        assert reasons == ["SERVER_UNREGISTERED", "APPROVAL_ABSENT"]
        assert len(calls) == 1
        assert calls[0]["server"] == {
            "id": "srv-1",
            "status": "unregistered",
            "transport": "http",
            "auth_type": "none",
            "provider_allowed": False,
            "auth_server_allowed": False,
        }

    def test_unregistered_server_is_allowed_in_observe_mode_with_findings_recorded(self, tmp_path):
        snap = snapshot(server=None, approvals=(), settings=SettingsRow(mode=Mode.OBSERVE))
        h = harness(tmp_path, snap, cfg=config(tmp_path, mode_ceiling=Mode.OBSERVE))
        outcome = decide(h)
        assert outcome.allowed
        assert outcome.snapshot.evaluated_decision is Action.BLOCK
        assert outcome.snapshot.enforced_action is Action.ALLOW

    def test_the_environment_ceiling_keeps_enforce_over_a_stored_observe(self, tmp_path):
        snap = snapshot(server=None, approvals=(), settings=SettingsRow(mode=Mode.OBSERVE))
        h = harness(tmp_path, snap, cfg=config(tmp_path, mode_ceiling=Mode.ENFORCE))
        outcome = decide(h)
        assert not outcome.allowed and outcome.snapshot.mode is Mode.ENFORCE

    @pytest.mark.parametrize(
        "field", ["url_sha256", "transport", "auth_type"], ids=["url", "transport", "auth"]
    )
    def test_any_contract_value_difference_is_an_identity_mismatch(self, tmp_path, field):
        other = {
            "url_sha256": url_sha256("https://other/"),
            "transport": "sse",
            "auth_type": "oauth2",
        }
        snap = snapshot(server=replace(server_row(), **{field: other[field]}))
        assert code(decide(harness(tmp_path, snap))) == "MINT-SERVER-IDENTITY-MISMATCH"

    def test_identity_mismatch_is_not_converted_in_observe_mode(self, tmp_path):
        snap = snapshot(server=server_row(transport="sse"), settings=SettingsRow(mode=Mode.OBSERVE))
        h = harness(tmp_path, snap, cfg=config(tmp_path, mode_ceiling=Mode.OBSERVE))
        assert code(decide(h)) == "MINT-SERVER-IDENTITY-MISMATCH"


class TestDenialAndApproval:
    @pytest.mark.parametrize(
        "row",
        [
            denial(scope_type="team", scope_id="t1"),
            denial(scope_type="user", scope_id="u1"),
            denial(scope_type="key", scope_id="k" * 64),
            denial(scope_type="org", scope_id="org1"),
            denial(scope_type="server", scope_id="srv-1", server_id="srv-1"),
            denial(tools=("search", "other")),
            denial(org_id="org1"),
            denial(server_id="srv-1"),
        ],
    )
    def test_matching_denials_stop_the_call(self, tmp_path, row):
        assert code(decide(harness(tmp_path, snapshot(denials=(row,))))) == "MINT-DENIED"

    @pytest.mark.parametrize(
        "row",
        [
            denial(scope_type="team", scope_id="t2"),
            denial(tools=("other",)),
            denial(org_id="org2"),
            denial(server_id="srv-2"),
            denial(scope_type="org", scope_id="org2"),
        ],
    )
    def test_non_matching_denials_do_not_apply(self, tmp_path, row):
        assert decide(harness(tmp_path, snapshot(denials=(row,)))).allowed

    def test_a_denial_beats_a_valid_approval_in_observe_mode(self, tmp_path):
        snap = snapshot(denials=(denial(),), settings=SettingsRow(mode=Mode.OBSERVE))
        h = harness(tmp_path, snap, cfg=config(tmp_path, mode_ceiling=Mode.OBSERVE))
        assert code(decide(h)) == "MINT-DENIED"

    def test_a_terminated_relation_stops_even_when_another_relation_has_a_valid_approval(
        self, tmp_path
    ):
        snap = snapshot(
            approvals=(
                approval(id="a-old", relation_id="r-old", scope_type="user", scope_id="u1"),
                approval(id="a-new", relation_id="r1"),
            ),
            relations={
                "r1": RelationRow("r1", "active"),
                "r-old": RelationRow("r-old", "terminating"),
            },
        )
        outcome = decide(harness(tmp_path, snap))
        assert code(outcome) == "MINT-RELATION-TERMINATED"

    def test_a_terminated_relation_stops_in_observe_mode(self, tmp_path):
        snap = snapshot(
            relations={"r1": RelationRow("r1", "terminated")},
            settings=SettingsRow(mode=Mode.OBSERVE),
        )
        h = harness(tmp_path, snap, cfg=config(tmp_path, mode_ceiling=Mode.OBSERVE))
        assert code(decide(h)) == "MINT-RELATION-TERMINATED"

    @pytest.mark.parametrize(
        "entry",
        [
            allowlist(status="lifted"),
            allowlist(status="expired"),
            allowlist(valid_until=NOW),
            allowlist(valid_from=NOW + timedelta(seconds=1)),
            None,
        ],
        ids=["lifted", "expired-status", "ended", "not-yet-valid", "missing"],
    )
    def test_an_unusable_basis_allowlist_stops_the_call(self, tmp_path, entry):
        server = server_row(basis_allowlist_ids=("al1",))
        snap = snapshot(server=server, allowlists={"al1": entry} if entry else {})
        assert code(decide(harness(tmp_path, snap))) == "MINT-ALLOWLIST-EXPIRED"

    def test_a_usable_basis_allowlist_passes_and_fills_the_policy_input(self, tmp_path):
        server = server_row(basis_allowlist_ids=("al1",))
        calls: list = []
        h = harness(
            tmp_path,
            snapshot(server=server, allowlists={"al1": allowlist()}),
            evaluate=evaluator(calls=calls),
        )
        assert decide(h).allowed
        assert calls[0]["server"]["provider_allowed"] is True
        assert calls[0]["server"]["auth_server_allowed"] is False

    def test_no_matching_approval_is_a_finding(self, tmp_path):
        outcome = decide(harness(tmp_path, snapshot(approvals=())))
        assert code(outcome) == "MINT-APPROVAL-ABSENT"
        assert outcome.snapshot.stop is None

    def test_a_revoked_only_history_stops(self, tmp_path):
        snap = snapshot(approvals=(approval(status="revoked"),))
        assert code(decide(harness(tmp_path, snap))) == "MINT-APPROVAL-REVOKED"

    def test_a_revoked_and_expired_history_reports_revocation(self, tmp_path):
        snap = snapshot(
            approvals=(
                approval(id="a1", status="revoked"),
                approval(id="a2", valid_until=NOW - timedelta(seconds=1)),
            )
        )
        assert code(decide(harness(tmp_path, snap))) == "MINT-APPROVAL-REVOKED"

    @pytest.mark.parametrize(
        "row",
        [
            approval(valid_until=NOW),
            approval(valid_until=NOW - timedelta(days=1)),
            approval(status="expired"),
        ],
        ids=["boundary", "past", "stored-status"],
    )
    def test_an_expired_only_history_stops(self, tmp_path, row):
        assert (
            code(decide(harness(tmp_path, snapshot(approvals=(row,))))) == "MINT-APPROVAL-EXPIRED"
        )

    def test_a_not_yet_valid_approval_is_not_a_grant_and_not_a_history(self, tmp_path):
        row = approval(valid_from=NOW + timedelta(seconds=1))
        outcome = decide(harness(tmp_path, snapshot(approvals=(row,))))
        assert code(outcome) == "MINT-APPROVAL-ABSENT" and outcome.snapshot.stop is None

    def test_valid_from_boundary_is_inclusive(self, tmp_path):
        row = approval(valid_from=NOW)
        assert decide(harness(tmp_path, snapshot(approvals=(row,)))).allowed

    def test_history_stops_in_observe_mode_but_absence_is_observable(self, tmp_path):
        cfg = config(tmp_path, mode_ceiling=Mode.OBSERVE)
        observe = SettingsRow(mode=Mode.OBSERVE)
        revoked = snapshot(approvals=(approval(status="revoked"),), settings=observe)
        assert code(decide(harness(tmp_path, revoked, cfg=cfg))) == "MINT-APPROVAL-REVOKED"
        absent = snapshot(approvals=(), settings=observe)
        assert decide(harness(tmp_path, absent, cfg=cfg)).allowed

    @pytest.mark.parametrize("mode", [Mode.ENFORCE, Mode.OBSERVE])
    def test_a_revocation_is_not_hidden_by_a_not_started_approval(self, tmp_path, mode):
        snap = snapshot(
            approvals=(
                approval(id="a1", status="revoked"),
                approval(id="a2", valid_from=NOW + timedelta(hours=1)),
            ),
            settings=SettingsRow(mode=mode),
        )
        h = harness(tmp_path, snap, cfg=config(tmp_path, mode_ceiling=mode))
        outcome = decide(h)
        assert code(outcome) == "MINT-APPROVAL-REVOKED" and not outcome.allowed
        assert outcome.snapshot.stop is not None

    def test_not_started_and_pending_approvals_alone_are_only_an_absence(self, tmp_path):
        snap = snapshot(
            approvals=(
                approval(id="a1", valid_from=NOW + timedelta(hours=1)),
                approval(id="a2", status="pending"),
            )
        )
        outcome = decide(harness(tmp_path, snap))
        assert code(outcome) == "MINT-APPROVAL-ABSENT" and outcome.snapshot.stop is None

    @pytest.mark.parametrize("mode", [Mode.ENFORCE, Mode.OBSERVE])
    def test_an_expiry_is_not_hidden_by_a_pending_approval(self, tmp_path, mode):
        snap = snapshot(
            approvals=(
                approval(id="a1", valid_until=NOW - timedelta(seconds=1)),
                approval(id="a2", status="pending"),
            ),
            settings=SettingsRow(mode=mode),
        )
        h = harness(tmp_path, snap, cfg=config(tmp_path, mode_ceiling=mode))
        outcome = decide(h)
        assert code(outcome) == "MINT-APPROVAL-EXPIRED" and outcome.snapshot.stop is not None

    def test_revocation_outranks_expiry_next_to_not_started_and_pending_approvals(self, tmp_path):
        snap = snapshot(
            approvals=(
                approval(id="a1", valid_until=NOW - timedelta(days=1)),
                approval(id="a2", status="revoked"),
                approval(id="a3", valid_from=NOW + timedelta(days=1)),
                approval(id="a4", status="pending"),
            )
        )
        assert code(decide(harness(tmp_path, snap))) == "MINT-APPROVAL-REVOKED"

    def test_a_tool_outside_every_valid_approval_is_a_finding(self, tmp_path):
        snap = snapshot(approvals=(approval(tools=("other",)),))
        outcome = decide(harness(tmp_path, snap))
        assert code(outcome) == "MINT-TOOL-NOT-IN-APPROVAL" and outcome.snapshot.stop is None

    def test_the_narrowest_scope_is_selected(self, tmp_path):
        snap = snapshot(
            approvals=(
                approval(id="a-org", scope_type="org", scope_id="org1"),
                approval(id="a-team", scope_type="team", scope_id="t1"),
                approval(id="a-user", scope_type="user", scope_id="u1"),
                approval(id="a-key", scope_type="key", scope_id="k" * 64),
            )
        )
        assert decide(harness(tmp_path, snap)).snapshot.selected_approval_id == "a-key"

    def test_within_a_scope_the_latest_valid_from_wins_then_the_id(self, tmp_path):
        older = approval(id="a-9", valid_from=NOW - timedelta(days=2))
        newer_b = approval(id="a-2", valid_from=NOW - timedelta(hours=1))
        newer_a = approval(id="a-1", valid_from=NOW - timedelta(hours=1))
        snap = snapshot(approvals=(older, newer_b, newer_a))
        assert decide(harness(tmp_path, snap)).snapshot.selected_approval_id == "a-1"

    def test_a_broader_approval_allows_when_the_narrower_one_is_revoked(self, tmp_path):
        snap = snapshot(
            approvals=(
                approval(id="a-user", scope_type="user", scope_id="u1", status="revoked"),
                approval(id="a-team"),
            )
        )
        outcome = decide(harness(tmp_path, snap))
        assert outcome.allowed and outcome.snapshot.selected_approval_id == "a-team"

    def test_a_narrower_scope_covers_a_broader_one(self, tmp_path):
        snap = snapshot(
            approvals=(
                approval(id="a-key", scope_type="key", scope_id="k" * 64, tools=("a",)),
                approval(id="a-team", tools=("a", "b")),
            )
        )
        calls: list = []
        h = harness(tmp_path, snap, evaluate=evaluator(calls=calls))
        outcome = decide(h, request(tool_name="b"))
        assert code(outcome) == "MINT-TOOL-NOT-IN-APPROVAL"
        assert outcome.snapshot.stop is None and outcome.snapshot.selected_approval_id is None
        assert calls[0]["approval"] == {"present": False}
        listed = decide(harness(tmp_path, snap), request(tool_name="a"))
        assert listed.snapshot.selected_approval_id == "a-key"

    def test_the_broader_approval_applies_once_the_narrower_one_is_revoked(self, tmp_path):
        snap = snapshot(
            approvals=(
                approval(
                    id="a-key", scope_type="key", scope_id="k" * 64, tools=("a",), status="revoked"
                ),
                approval(id="a-team", tools=("a", "b")),
            )
        )
        outcome = decide(harness(tmp_path, snap), request(tool_name="b"))
        assert outcome.allowed and outcome.snapshot.selected_approval_id == "a-team"

    @pytest.mark.parametrize(
        "narrow",
        [
            {"status": "revoked"},
            {"valid_until": NOW - timedelta(seconds=1)},
            {"valid_from": NOW + timedelta(seconds=1)},
        ],
        ids=["revoked", "expired", "not-started"],
    )
    def test_an_invalid_narrower_approval_does_not_cover_the_broader_one(self, tmp_path, narrow):
        snap = snapshot(
            approvals=(
                approval(id="a-key", scope_type="key", scope_id="k" * 64, tools=("a",), **narrow),
                approval(id="a-team", tools=("a", "b")),
            )
        )
        outcome = decide(harness(tmp_path, snap), request(tool_name="b"))
        assert outcome.allowed and outcome.snapshot.selected_approval_id == "a-team"

    def test_a_broader_approval_applies_when_there_is_no_narrower_one(self, tmp_path):
        snap = snapshot(approvals=(approval(id="a-team", tools=("a", "b")),))
        outcome = decide(harness(tmp_path, snap), request(tool_name="b"))
        assert outcome.allowed and outcome.snapshot.selected_approval_id == "a-team"

    def test_within_one_scope_the_approval_that_lists_the_tool_is_chosen(self, tmp_path):
        snap = snapshot(
            approvals=(
                approval(id="a-new", tools=("other",), valid_from=NOW - timedelta(hours=1)),
                approval(id="a-old", tools=("search",), valid_from=NOW - timedelta(days=5)),
            )
        )
        outcome = decide(harness(tmp_path, snap))
        assert outcome.allowed and outcome.snapshot.selected_approval_id == "a-old"

    def test_the_org_scope_is_consulted_only_when_nothing_narrower_is_valid(self, tmp_path):
        snap = snapshot(
            approvals=(
                approval(id="a-org", scope_type="org", scope_id="org1", tools=("a", "b")),
                approval(id="a-user", scope_type="user", scope_id="u1", tools=("a",)),
            )
        )
        blocked = decide(harness(tmp_path, snap), request(tool_name="b"))
        assert code(blocked) == "MINT-TOOL-NOT-IN-APPROVAL"
        only_org = snapshot(approvals=(approval(id="a-org", scope_type="org", scope_id="org1"),))
        assert decide(harness(tmp_path, only_org)).snapshot.selected_approval_id == "a-org"

    def test_observe_mode_converts_the_missing_tool_finding_of_a_covering_scope(self, tmp_path):
        snap = snapshot(
            approvals=(
                approval(id="a-key", scope_type="key", scope_id="k" * 64, tools=("a",)),
                approval(id="a-team", tools=("a", "b")),
            ),
            settings=SettingsRow(mode=Mode.OBSERVE),
        )
        h = harness(tmp_path, snap, cfg=config(tmp_path, mode_ceiling=Mode.OBSERVE))
        outcome = decide(h, request(tool_name="b"))
        assert outcome.allowed and outcome.snapshot.selected_approval_id is None

    def test_approval_for_another_principal_scope_does_not_match(self, tmp_path):
        snap = snapshot(approvals=(approval(scope_id="t2"),))
        assert code(decide(harness(tmp_path, snap))) == "MINT-APPROVAL-ABSENT"

    def test_a_team_key_matches_key_and_team_scopes_but_not_user_scope(self, tmp_path):
        principal = user_principal(user_id=None)
        snap = snapshot(approvals=(approval(scope_type="user", scope_id="u1"),))
        assert code(decide(harness(tmp_path, snap), request(principal))) == "MINT-APPROVAL-ABSENT"
        assert decide(harness(tmp_path), request(principal)).allowed

    def test_a_service_account_marker_follows_the_team_key_rules(self, tmp_path):
        principal = user_principal(user_id=None, metadata={"service_account_id": "bot"})
        assert principal.principal_type.value == "service_account"
        assert decide(harness(tmp_path), request(principal)).allowed


class TestPolicyStage:
    def test_a_block_decision_is_a_policy_finding_that_enforce_mode_blocks(self, tmp_path):
        evaluate = evaluator("Block", policy_ids=("R3",), observable=True)
        outcome = decide(harness(tmp_path, evaluate=evaluate))
        assert code(outcome) == "MINT-POLICY-DETECT-R3"
        assert outcome.snapshot.stop is None
        assert outcome.snapshot.policy_decision.value == "Block"
        assert outcome.snapshot.policy_ids == ("R3",)

    def test_observe_mode_converts_only_policies_that_are_observable_and_selected(self, tmp_path):
        cfg = config(tmp_path, mode_ceiling=Mode.OBSERVE)
        for ids, observable, allowed in [
            (frozenset({"R3"}), True, True),
            (frozenset({"R3"}), False, False),
            (frozenset(), True, False),
            (frozenset({"R1"}), True, False),
        ]:
            settings = SettingsRow(
                mode=Mode.OBSERVE,
                observable_reasons=OBSERVATION_CANDIDATES,
                observable_policy_ids=ids,
            )
            evaluate = evaluator("Block", policy_ids=("R3",), observable=observable)
            h = harness(tmp_path, snapshot(settings=settings), evaluate=evaluate, cfg=cfg)
            assert decide(h).allowed is allowed, (ids, observable)

    def test_an_observe_reason_removed_from_the_settings_is_blocked(self, tmp_path):
        settings = SettingsRow(
            mode=Mode.OBSERVE, observable_reasons=frozenset({Reason.SERVER_UNREGISTERED})
        )
        h = harness(
            tmp_path,
            snapshot(approvals=(), settings=settings),
            cfg=config(tmp_path, mode_ceiling=Mode.OBSERVE),
        )
        assert code(decide(h)) == "MINT-APPROVAL-ABSENT"

    def test_the_error_code_is_the_first_finding_that_was_not_converted(self, tmp_path):
        settings = SettingsRow(
            mode=Mode.OBSERVE,
            observable_reasons=frozenset({Reason.APPROVAL_ABSENT, Reason.POLICY_DETECT}),
            observable_policy_ids=frozenset(),
        )
        snap = snapshot(server=None, approvals=(), settings=settings)
        evaluate = evaluator("Block", policy_ids=("R1",), observable=True)
        h = harness(
            tmp_path, snap, evaluate=evaluate, cfg=config(tmp_path, mode_ceiling=Mode.OBSERVE)
        )
        outcome = decide(h)
        reasons = [f.reason_code for f in outcome.snapshot.findings]
        assert reasons == ["SERVER_UNREGISTERED", "APPROVAL_ABSENT", "POLICY_DETECT_R1"]
        assert code(outcome) == "MINT-SERVER-UNREGISTERED"

    def test_a_stop_reason_wins_over_earlier_findings(self, tmp_path):
        snap = snapshot(server=None, approvals=(), denials=(denial(),))
        outcome = decide(harness(tmp_path, snap))
        assert code(outcome) == "MINT-DENIED"
        assert [f.reason_code for f in outcome.snapshot.findings] == ["SERVER_UNREGISTERED"]

    def test_blocking_reason_skips_converted_findings(self):
        findings = (
            StageResult.finding(Reason.APPROVAL_ABSENT),
            StageResult.finding(Reason.TOOL_NOT_IN_APPROVAL),
        )
        from mint_litellm.types.decision import conclude

        observation = ObservationSettings(observable_reasons=frozenset({Reason.APPROVAL_ABSENT}))
        conclusion = conclude(findings, mode=Mode.OBSERVE, observation=observation)
        reason = blocking_reason(
            conclusion, mode=Mode.OBSERVE, observation=observation, policy_observable=False
        )
        assert reason == "TOOL_NOT_IN_APPROVAL"

    def test_the_policy_decision_is_not_evaluated_when_an_earlier_stage_stops(self, tmp_path):
        calls: list = []
        h = harness(tmp_path, snapshot(denials=(denial(),)), evaluate=evaluator(calls=calls))
        outcome = decide(h)
        assert calls == []
        stages = {e.stage: e for e in outcome.snapshot.stages}
        assert stages[Stage.POLICY].status is StageStatus.SKIPPED
        assert stages[Stage.POLICY].missing_reason == "after_stop"
        assert outcome.snapshot.missing_reasons["policy_decision"] == "policy_not_evaluated"

    def test_the_contract_stage_is_recorded_as_skipped_but_does_not_stop(self, tmp_path):
        outcome = decide(harness(tmp_path))
        entry = next(e for e in outcome.snapshot.stages if e.stage is Stage.CONTRACT)
        assert entry.status is StageStatus.SKIPPED and entry.missing_reason == "unsupported_stage"

    def test_no_active_policy_version_stops(self, tmp_path):
        h = harness(tmp_path, snapshot(policy_version=None))
        assert code(decide(h)) == "MINT-POLICY-VERSION-MISMATCH"

    def test_an_evaluator_answering_for_another_version_stops(self, tmp_path):
        h = harness(tmp_path, evaluate=evaluator(version="pv2"))
        assert code(decide(h)) == "MINT-POLICY-VERSION-MISMATCH"
        h = harness(tmp_path, evaluate=evaluator(bundle="c" * 64))
        assert code(decide(h)) == "MINT-POLICY-VERSION-MISMATCH"

    def test_an_evaluation_failure_is_passed_through(self, tmp_path):
        failure = StageResult.stop(Reason.DEPENDENCY_FAILURE)
        outcome = decide(harness(tmp_path, evaluate=evaluator(failure=failure)))
        assert code(outcome) == "MINT-DEPENDENCY-OPA" and outcome.error.retry_safe is True

    def test_an_evaluator_exception_is_an_opa_dependency_failure(self, tmp_path):
        async def broken(policy_input, **kwargs):
            raise ConnectionError("opa down")

        outcome = decide(harness(tmp_path, evaluate=broken))
        assert code(outcome) == "MINT-DEPENDENCY-OPA"

    def test_an_evaluator_over_budget_is_an_opa_dependency_failure(self, tmp_path):
        async def slow(policy_input, **kwargs):
            await asyncio.sleep(1)

        h = harness(tmp_path, evaluate=slow, cfg=config(tmp_path, decision_budget_seconds=0.05))
        assert code(decide(h)) == "MINT-DEPENDENCY-OPA"

    def test_a_missing_evaluator_is_an_opa_dependency_failure(self, tmp_path):
        assert code(decide(harness(tmp_path, evaluate=None))) == "MINT-DEPENDENCY-OPA"

    def test_missing_policy_inputs_are_a_schema_error(self, tmp_path):
        h = harness(tmp_path, evaluate=evaluator(missing_inputs=("approval",)))
        assert code(decide(h)) == "MINT-SCHEMA-ERROR"

    def test_an_unknown_decision_is_a_schema_error(self, tmp_path):
        assert (
            code(decide(harness(tmp_path, evaluate=evaluator("Restrict")))) == "MINT-SCHEMA-ERROR"
        )

    def test_an_unsupported_obligation_stops_at_the_obligation_stage(self, tmp_path):
        evaluate = evaluator("Allow", obligations=({"type": "redact", "fields": ["a"]},))
        outcome = decide(harness(tmp_path, evaluate=evaluate))
        assert code(outcome) == "MINT-UNSUPPORTED"
        stages = {e.stage: e for e in outcome.snapshot.stages}
        assert stages[Stage.OBLIGATIONS].status is StageStatus.STOP

    def test_an_unexpected_exception_blocks_with_a_schema_error_and_still_records(self, tmp_path):
        snap = snapshot(approvals=(approval(conditions="not a mapping"),))
        h = harness(tmp_path, snap)
        outcome = decide(h)
        assert code(outcome) == "MINT-SCHEMA-ERROR"
        assert outcome.snapshot.stop.detail.startswith("internal ")
        assert len(h.store.of_kind("decision_evaluated")) == 1


class TestObligations:
    REQUIRE = ({"type": "require_call_approval"},)

    def approval_snapshot(self, **override):
        return snapshot(call_approvals=(call_approval(),), **override)

    def test_a_high_risk_decision_requires_a_matching_call_approval(self, tmp_path):
        evaluate = evaluator("RequireApproval", policy_ids=("R2",))
        outcome = decide(harness(tmp_path, snapshot(), evaluate=evaluate))
        assert code(outcome) == "MINT-CALL-APPROVAL-REQUIRED"
        assert outcome.snapshot.obligation_types[0].value == "require_call_approval"

    def test_a_matching_call_approval_allows_and_is_consumed_with_the_decision(self, tmp_path):
        evaluate = evaluator("RequireApproval", policy_ids=("R2",))
        h = harness(tmp_path, self.approval_snapshot(), evaluate=evaluate)
        outcome = decide(h)
        assert outcome.allowed
        assert len(h.store.consumed) == 1
        consumed = h.store.consumed[0]
        assert str(consumed.call_approval_id) == "00000000-0000-0000-0000-0000000000c1"
        assert consumed.invocation_id == outcome.snapshot.invocation_id
        assert consumed.state_version == 2
        assert outcome.snapshot.consumed_call_approval_id == str(consumed.call_approval_id)
        assert len(h.store.of_kind("decision_evaluated")) == 1

    def test_a_blocked_call_never_consumes_the_call_approval(self, tmp_path):
        evaluate = evaluator("RequireApproval", policy_ids=("R2",))
        snap = self.approval_snapshot(denials=(denial(),))
        h = harness(tmp_path, snap, evaluate=evaluate)
        assert code(decide(h)) == "MINT-DENIED"
        assert h.store.consumed == []

    def test_a_finding_that_blocks_does_not_consume_either(self, tmp_path):
        evaluate = evaluator("RequireApproval", policy_ids=("R2",))
        snap = self.approval_snapshot(approvals=())
        h = harness(tmp_path, snap, evaluate=evaluate)
        assert not decide(h).allowed
        assert h.store.consumed == []

    @pytest.mark.parametrize(
        "row",
        [
            call_approval(tool="other"),
            call_approval(args_fp="0" * 64),
            call_approval(principal_id="u2"),
            call_approval(principal_type="team_key"),
            call_approval(server_id="srv-2"),
            call_approval(org_id="org2"),
            call_approval(fp_key_version=2),
            call_approval(contract_version="v1"),
            call_approval(expires_at=NOW),
        ],
        ids=[
            "tool",
            "args",
            "principal",
            "principal-type",
            "server",
            "org",
            "key-version",
            "contract",
            "expired",
        ],
    )
    def test_a_call_approval_that_differs_in_any_condition_does_not_match(self, tmp_path, row):
        evaluate = evaluator("RequireApproval", policy_ids=("R2",))
        h = harness(tmp_path, snapshot(call_approvals=(row,)), evaluate=evaluate)
        assert code(decide(h)) == "MINT-CALL-APPROVAL-REQUIRED"

    def test_a_call_approval_from_another_policy_version_is_stale(self, tmp_path):
        evaluate = evaluator("RequireApproval", policy_ids=("R2",))
        snap = snapshot(call_approvals=(call_approval(policy_version_id="pv0"),))
        assert (
            code(decide(harness(tmp_path, snap, evaluate=evaluate))) == "MINT-CALL-APPROVAL-STALE"
        )

    def test_without_a_fingerprint_key_the_call_approval_cannot_match(self, tmp_path):
        evaluate = evaluator("RequireApproval", policy_ids=("R2",))
        cfg = config(tmp_path, fingerprint_key=None)
        h = harness(tmp_path, self.approval_snapshot(), evaluate=evaluate, cfg=cfg)
        assert code(decide(h)) == "MINT-CALL-APPROVAL-REQUIRED"

    def test_the_argument_fingerprint_is_stable_under_key_order(self):
        a = argument_fingerprint({"a": 1, "b": {"y": [1, 2], "x": "한글"}}, FP_KEY)
        b = argument_fingerprint({"b": {"x": "한글", "y": [1, 2]}, "a": 1}, FP_KEY)
        assert a == b and len(a) == 64
        assert a != argument_fingerprint({"a": 1, "b": {"y": [2, 1], "x": "한글"}}, FP_KEY)
        assert a != argument_fingerprint(
            {"a": 1, "b": {"y": [1, 2], "x": "한글"}}, bytes(range(1, 33))
        )

    def test_a_lost_consumption_blocks_and_records_the_block_without_consuming(self, tmp_path):
        evaluate = evaluator("RequireApproval", policy_ids=("R2",))
        h = harness(tmp_path, self.approval_snapshot(), evaluate=evaluate)
        h.store.lose_consumption = True
        outcome = decide(h)
        assert code(outcome) == "MINT-CALL-APPROVAL-REQUIRED"
        assert h.store.consumed == []
        events = h.store.of_kind("decision_evaluated")
        assert len(events) == 1 and events[0].payload["enforced_action"] == "block"
        assert outcome.snapshot.consumed_call_approval_id is None

    def test_consent_obligation_states(self, tmp_path):
        evaluate = evaluator(
            "Allow", obligations=({"type": "require_consent", "purpose": "support"},)
        )
        cases = [
            ((), "MINT-CONSENT-REQUIRED"),
            ((consent(valid_until=NOW),), "MINT-CONSENT-EXPIRED"),
            ((consent(revoked_at=NOW - timedelta(hours=1)),), "MINT-CONSENT-REVOKED"),
            ((consent(granted_at=NOW + timedelta(hours=1)),), "MINT-CONSENT-EXPIRED"),
            ((consent(purpose="other"),), "MINT-CONSENT-REQUIRED"),
            ((consent(org_id="org2"),), "MINT-CONSENT-REQUIRED"),
            ((consent(),), None),
        ]
        for rows, expected in cases:
            h = harness(tmp_path, snapshot(consents=rows), evaluate=evaluate)
            outcome = decide(h)
            assert (None if outcome.allowed else code(outcome)) == expected, rows

    def test_consent_cannot_be_granted_to_a_team_key(self, tmp_path):
        evaluate = evaluator(
            "Allow", obligations=({"type": "require_consent", "purpose": "support"},)
        )
        principal = user_principal(user_id=None)
        h = harness(tmp_path, snapshot(consents=(consent(),)), evaluate=evaluate)
        assert code(decide(h, request(principal))) == "MINT-CONSENT-REQUIRED"

    def test_a_revoked_consent_is_not_renewed_by_an_older_live_one(self, tmp_path):
        evaluate = evaluator(
            "Allow", obligations=({"type": "require_consent", "purpose": "support"},)
        )
        rows = (consent(id="c1"), consent(id="c2", revoked_at=NOW - timedelta(hours=1)))
        outcome = decide(harness(tmp_path, snapshot(consents=rows), evaluate=evaluate))
        assert outcome.allowed

    def test_conflicting_consent_purposes_are_unsupported(self, tmp_path):
        obligations = (
            {"type": "require_consent", "purpose": "a"},
            {"type": "require_consent", "purpose": "b"},
        )
        outcome = decide(harness(tmp_path, evaluate=evaluator("Allow", obligations=obligations)))
        assert code(outcome) == "MINT-UNSUPPORTED"

    def test_alert_obligation_is_recorded_by_type_only(self, tmp_path):
        evaluate = evaluator("Alert", obligations=({"type": "alert", "severity": "high"},))
        outcome = decide(harness(tmp_path, evaluate=evaluate))
        assert outcome.allowed
        assert [t.value for t in outcome.snapshot.obligation_types] == ["alert"]
        assert "high" not in json.dumps(outcome.snapshot.model_dump(mode="json"))


class TestRecording:
    def test_a_database_failure_on_the_allow_path_blocks_with_audit_failure(self, tmp_path):
        h = harness(tmp_path)
        h.store.fail = RuntimeError("write failed")
        outcome = decide(h)
        assert code(outcome) == "MINT-AUDIT-FAILURE"
        assert outcome.evidence.value == "buffer"
        events = buffered(h)
        assert len(events) == 1
        recorded = events[0].payload
        assert (
            recorded["enforced_action"] == "block"
            and recorded["stop"]["reason_code"] == "AUDIT_FAILURE"
        )

    def test_allow_path_with_no_database_and_no_buffer_is_blocked_and_logged_to_stderr(
        self, tmp_path
    ):
        h = harness(tmp_path)
        h.store.fail = RuntimeError("write failed")
        blocker = tmp_path / "plainfile"
        blocker.write_text("x")
        h.buffer._dir = blocker / "inner"
        outcome = decide(h)
        assert code(outcome) == "MINT-AUDIT-FAILURE" and outcome.evidence.value == "stderr"
        lines = h.stderr.getvalue().strip().splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["payload"]["enforced_action"] == "block"

    def test_a_block_path_database_failure_keeps_the_original_reason(self, tmp_path):
        h = harness(tmp_path, snapshot(denials=(denial(),)))
        h.store.fail = RuntimeError("write failed")
        outcome = decide(h)
        assert code(outcome) == "MINT-DENIED" and outcome.evidence.value == "buffer"

    def test_recording_over_budget_counts_as_a_failure(self, tmp_path):
        h = harness(tmp_path, cfg=config(tmp_path))
        h.deps.recorder._budget = 0.05

        async def slow(event, consume=None):
            await asyncio.sleep(1)

        h.store.insert_event = slow
        outcome = decide(h)
        assert code(outcome) == "MINT-AUDIT-FAILURE" and outcome.evidence.value == "buffer"

    def test_every_path_records_one_decision_event_with_a_unique_source_key(self, tmp_path):
        h = harness(tmp_path)
        for _ in range(3):
            decide(h)
        decide(h, request(user_principal(user_id=None, team_id=None)))
        keys = {e.source_key for e in h.store.of_kind("decision_evaluated")}
        assert len(h.store.of_kind("decision_evaluated")) == 4 == len(keys)


class TestPolicyInput:
    def build(self, **override):
        snap = override.pop("snap", snapshot())
        principal = override.pop("principal", None)
        from mint_litellm.plugin.pipeline import derive_principal

        base = principal or user_principal()
        derived = derive_principal(snap, base)
        selected = override.pop("selected", snap.approvals[0] if snap.approvals else None)
        return build_policy_input(
            snapshot=snap,
            principal=derived,
            resolved=resolved_server(),
            tool="search",
            arguments=override.pop("arguments", {"q": "secret value", "n": 3}),
            selected=selected,
            mode=Mode.ENFORCE,
            operation=OperationType.TOOL_CALL,
            high_risk_tools=frozenset({"b", "a"}),
        )

    def test_the_input_has_exactly_the_contract_keys(self):
        doc = self.build()
        assert set(doc) == {
            "principal",
            "server",
            "tool",
            "arguments",
            "approval",
            "consent",
            "time",
            "settings",
            "mode",
            "operation",
        }
        assert doc["principal"] == {
            "type": "user",
            "id": "u1",
            "key_id": "k" * 64,
            "org_id": "org1",
            "org_source": "team",
            "team_id": "t1",
            "claimed_end_user_id": None,
        }
        assert set(doc["server"]) == {
            "id",
            "status",
            "transport",
            "auth_type",
            "provider_allowed",
            "auth_server_allowed",
        }
        assert doc["tool"] == {"name": "search"}
        assert doc["settings"] == {"high_risk_tools": ["a", "b"]}
        assert doc["mode"] == "enforce" and doc["operation"] == "tool_call"

    def test_arguments_carry_keys_and_sizes_but_never_values(self):
        doc = self.build(arguments={"q": "secret value", "n": 3, "nested": {"k": "v"}})
        assert doc["arguments"]["keys"] == ["n", "nested", "q"]
        assert doc["arguments"]["sizes"]["q"] == len("secret value")
        assert "secret" not in json.dumps(doc)
        assert set(doc["arguments"]) == {"keys", "sizes"}

    def test_approval_summary_and_expiry_days(self):
        doc = self.build(snap=snapshot(approvals=(approval(conditions={"a": 1}),)))
        assert doc["approval"] == {
            "present": True,
            "id": "00000000-0000-0000-0000-0000000000a1",
            "class": "standard",
            "scope_type": "team",
            "expires_in_days": 30,
            "conditions": {"a": 1},
        }
        assert doc["consent"] == {"status": "not_applicable", "source": None}

    def test_without_an_approval_only_presence_is_reported(self):
        doc = self.build(snap=snapshot(approvals=()), selected=None)
        assert doc["approval"] == {"present": False}

    def test_time_is_the_snapshot_clock_in_utc_nanoseconds(self):
        doc = self.build()
        assert doc["time"]["iso"] == "2026-11-03T09:00:00+00:00"
        assert doc["time"]["ns"] == 1_793_696_400_000_000_000

    def test_consent_state_follows_the_selected_approvals_purpose(self):
        conditions = {"require_consent": "support"}
        snap = snapshot(approvals=(approval(conditions=conditions),), consents=(consent(),))
        assert self.build(snap=snap)["consent"] == {
            "status": "granted",
            "source": "operator_attested",
        }
        snap = snapshot(approvals=(approval(conditions=conditions),), consents=())
        assert self.build(snap=snap)["consent"] == {"status": "absent", "source": None}

    def test_a_non_string_consent_condition_is_rejected(self, tmp_path):
        snap = snapshot(approvals=(approval(conditions={"require_consent": True}),))
        outcome = decide(harness(tmp_path, snap))
        assert code(outcome) == "MINT-SCHEMA-ERROR"

    def test_the_claimed_end_user_is_passed_as_a_claim(self):
        principal = user_principal(claimed_end_user_id="visitor-9")
        assert self.build(principal=principal)["principal"]["claimed_end_user_id"] == "visitor-9"


class TestDiscoveryOperation:
    def test_a_listed_tool_with_a_grant_is_shown_and_recorded_as_a_discovery_event(self, tmp_path):
        h = harness(tmp_path)
        outcome = decide(h, request(operation=OperationType.RUNTIME_DISCOVERY))
        assert outcome.allowed
        assert len(h.store.of_kind("discovery_evaluated")) == 1
        assert h.store.of_kind("decision_evaluated") == []

    def test_discovery_does_not_evaluate_the_policy(self, tmp_path):
        calls: list = []
        h = harness(tmp_path, evaluate=evaluator("Block", policy_ids=("R1",), calls=calls))
        assert decide(h, request(operation=OperationType.RUNTIME_DISCOVERY)).allowed
        assert calls == []

    def test_a_tool_without_a_grant_is_hidden_in_enforce_mode(self, tmp_path):
        h = harness(tmp_path, snapshot(approvals=()))
        outcome = decide(h, request(operation=OperationType.RUNTIME_DISCOVERY))
        assert not outcome.allowed and code(outcome) == "MINT-APPROVAL-ABSENT"

    def test_observe_mode_shows_observable_findings_but_hides_stops(self, tmp_path):
        cfg = config(tmp_path, mode_ceiling=Mode.OBSERVE)
        settings = SettingsRow(mode=Mode.OBSERVE)
        shown = harness(tmp_path, snapshot(approvals=(), settings=settings), cfg=cfg)
        assert decide(shown, request(operation=OperationType.RUNTIME_DISCOVERY)).allowed
        hidden = harness(tmp_path, snapshot(denials=(denial(),), settings=settings), cfg=cfg)
        assert not decide(hidden, request(operation=OperationType.RUNTIME_DISCOVERY)).allowed

    def test_a_listing_is_hidden_while_unverified(self, tmp_path):
        h = harness(tmp_path, verified=False)
        outcome = decide(h, request(operation=OperationType.RUNTIME_DISCOVERY))
        assert code(outcome) == "MINT-ENFORCEMENT-UNVERIFIED"


class TestDecisionBudget:
    """판정 하나의 시간 예산을 상태 조회, 정책 평가, 선기록이 나눠 쓴다."""

    def test_the_deadline_only_runs_down(self):
        deadline = Deadline(0.05)
        first = deadline.remaining()
        time_left = [first]
        for _ in range(3):
            asyncio.run(asyncio.sleep(0.02))
            time_left.append(deadline.remaining())
        assert time_left == sorted(time_left, reverse=True) and time_left[-1] == 0.0

    def test_the_policy_engine_only_gets_what_the_snapshot_left(self, tmp_path):
        cfg = config(tmp_path, decision_budget_seconds=0.3)
        seen = {}

        async def evaluate(policy_input, *, policy_version_id, bundle_sha256):
            started = asyncio.get_running_loop().time()
            try:
                await asyncio.sleep(0.25)
            finally:
                seen["waited"] = asyncio.get_running_loop().time() - started
            raise AssertionError("must have been cancelled")

        h = harness(tmp_path, evaluate=evaluate, cfg=cfg)
        original = h.snapshots.read_snapshot

        async def slow_snapshot(query):
            await asyncio.sleep(0.15)
            return await original(query)

        h.snapshots.read_snapshot = slow_snapshot
        outcome = decide(h)
        assert code(outcome) == "MINT-DEPENDENCY-OPA"
        assert seen["waited"] < 0.2

    def test_an_insert_that_outlives_the_remaining_budget_is_unknown_and_blocks_the_call(
        self, tmp_path
    ):
        cfg = config(tmp_path, decision_budget_seconds=0.2)
        h = harness(tmp_path, cfg=cfg)
        h.deps.recorder._budget = 5.0

        async def slow_insert(event, consume=None):
            await asyncio.sleep(1)

        h.store.insert_event = slow_insert
        outcome = decide(h)
        assert code(outcome) == "MINT-AUDIT-FAILURE" and outcome.evidence.value == "buffer"
        (event,) = buffered(h)
        assert event.source_key.endswith(":decision_evaluated")
        assert event.payload["stop"]["reason_code"] == "AUDIT_FAILURE"

    def test_an_unsupported_answer_from_the_policy_engine_is_an_obligation_stage_stop(
        self, tmp_path
    ):
        failure = StageResult.stop(Reason.UNSUPPORTED)
        outcome = decide(harness(tmp_path, evaluate=evaluator(failure=failure)))
        assert code(outcome) == "MINT-UNSUPPORTED"
        stages = {e.stage: e for e in outcome.snapshot.stages}
        assert stages[Stage.OBLIGATIONS].status is StageStatus.STOP


def test_the_policy_input_passes_the_policy_modules_own_field_check(tmp_path):
    calls: list = []
    assert decide(harness(tmp_path, evaluate=evaluator(calls=calls))).allowed
    assert has_only_policy_fields(calls[0])
    unregistered = snapshot(server=None, approvals=())
    decide(harness(tmp_path, unregistered, evaluate=evaluator(calls=calls)))
    assert has_only_policy_fields(calls[1]) and calls[1]["approval"] == {"present": False}


def test_missing_principal_values_are_left_out_of_the_policy_input(tmp_path):
    calls: list = []
    principal = user_principal(user_id=None, claimed_end_user_id=None)
    assert decide(harness(tmp_path, evaluate=evaluator(calls=calls)), request(principal)).allowed
    sent = calls[0]["principal"]
    assert sent["type"] == "team_key" and sent["id"] == "k" * 64
    assert sent["claimed_end_user_id"] is None and sent["team_id"] == "t1"


class TestUnregisteredServer:
    """MINT 서버 행이 없어도 정책은 server 객체를 받아 관측 모드 전환이 막히지 않는다."""

    def block_r1(self):
        return evaluator("Block", policy_ids=("R1",), observable=True)

    def test_observe_mode_records_every_finding_and_allows(self, tmp_path):
        settings = SettingsRow(
            mode=Mode.OBSERVE, observable_policy_ids=frozenset({"R1"}), settings_version=2
        )
        snap = snapshot(server=None, approvals=(), settings=settings)
        h = harness(
            tmp_path,
            snap,
            evaluate=self.block_r1(),
            cfg=config(tmp_path, mode_ceiling=Mode.OBSERVE),
        )
        outcome = decide(h)
        assert outcome.allowed
        assert [f.reason_code for f in outcome.snapshot.findings] == [
            "SERVER_UNREGISTERED",
            "APPROVAL_ABSENT",
            "POLICY_DETECT_R1",
        ]
        assert outcome.snapshot.evaluated_decision is Action.BLOCK
        assert outcome.snapshot.enforced_action is Action.ALLOW
        assert outcome.snapshot.stop is None
        (event,) = h.store.of_kind("decision_evaluated")
        assert len(event.payload["findings"]) == 3

    def test_enforce_mode_reports_the_first_finding(self, tmp_path):
        snap = snapshot(server=None, approvals=())
        outcome = decide(harness(tmp_path, snap, evaluate=self.block_r1()))
        assert code(outcome) == "MINT-SERVER-UNREGISTERED"
        assert [f.reason_code for f in outcome.snapshot.findings][-1] == "POLICY_DETECT_R1"

    def test_the_policy_input_names_the_server_as_unregistered(self, tmp_path):
        calls: list = []
        snap = snapshot(server=None, approvals=())
        decide(harness(tmp_path, snap, evaluate=evaluator(calls=calls)))
        assert calls[0]["server"]["status"] == "unregistered"
        assert has_only_policy_fields(calls[0])

    def test_a_registered_server_reports_its_state_and_active_allowlist_kinds(self):
        server = server_row(basis_allowlist_ids=("al1", "al2"), status="suspended")
        allowlists = {
            "al1": allowlist(id="al1", kind="provider"),
            "al2": allowlist(id="al2", kind="auth_server"),
        }
        doc = TestPolicyInput().build(snap=snapshot(server=server, allowlists=allowlists))
        assert doc["server"]["status"] == "suspended"
        assert doc["server"]["provider_allowed"] is True
        assert doc["server"]["auth_server_allowed"] is True

    def test_a_lifted_allowlist_entry_does_not_count(self):
        server = server_row(basis_allowlist_ids=("al1",))
        snap = snapshot(server=server, allowlists={"al1": allowlist(status="lifted")})
        assert TestPolicyInput().build(snap=snap)["server"]["provider_allowed"] is False

    def test_a_server_without_a_url_is_unsupported(self, tmp_path):
        def resolver(hook):
            raise ServerResolutionError(Reason.UNSUPPORTED, "server_without_url")

        req = build_request(
            {"mcp_server_name": "local", "mcp_tool_name": "t", "mcp_arguments": {}},
            None,
            operation=OperationType.TOOL_CALL,
            instance_id="inst",
            hook_parser=lambda d: HookData("local", "t", {}, None, None),
            resolver=resolver,
            principal_factory=lambda auth, instance_id: user_principal(),
        )
        h = harness(tmp_path)
        outcome = decide(h, req)
        assert code(outcome) == "MINT-UNSUPPORTED"
        assert outcome.snapshot.stop.detail == "server_without_url"
        assert h.snapshots.queries == []
        assert len(h.store.of_kind("decision_evaluated")) == 1
