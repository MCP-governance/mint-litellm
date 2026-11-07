"""본인 확인 판정 경로. 표시 값은 요청 표로만 보내고 사건에는 남기지 않는다."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from plugin_helpers import (
    FP_KEY,
    KEY,
    NOW,
    call_approval,
    config,
    evaluator,
    harness,
    request,
    snapshot,
    user_principal,
)

from mint_litellm.plugin.buffer import ConsumptionLost
from mint_litellm.plugin.obligations import RequireUserConfirmation, compose_obligations
from mint_litellm.plugin.pipeline import confirmation_display, evaluate_request
from mint_litellm.plugin.snapshot import ConfirmationRow
from mint_litellm.types.fingerprint import argument_fingerprint
from mint_litellm.types.reason_codes import Reason
from mint_litellm.types.settings import Mode

CONFIRM = {"type": "require_user_confirmation", "purpose": "전송", "display_fields": ["q"]}


def confirmation(**override):
    return ConfirmationRow(**{
        "id": uuid4(), "org_id": "org1", "principal_id": "u1", "server_id": "srv-1",
        "tool": "search", "args_fp": argument_fingerprint({"q": "x"}, FP_KEY),
        "fp_key_version": 1, "policy_version_id": "pv1", "expires_at": NOW + timedelta(minutes=9),
        "state_version": 2, "confirmed_by_user_id": "u1", "confirmed_by_key_token": "console",
        "requested_by_key_token": KEY, **override,
    })


def setup(tmp_path, snap=None, **kwargs):
    h = harness(tmp_path, snap, evaluate=evaluator(obligations=(CONFIRM,)), **kwargs)
    h.snapshots.confirmations = []

    async def create(params):
        h.snapshots.confirmations.append(params)

    h.snapshots.request_confirmation = create
    return h


def test_human_stops_and_requests_only_selected_display_values(tmp_path):
    h = setup(tmp_path, cfg=config(tmp_path, mode_ceiling=Mode.OBSERVE))
    result = asyncio.run(evaluate_request(
        request(arguments={"q": "비밀" * 150, "hidden": "x"}), h.deps,
    ))
    assert not result.allowed and result.snapshot.stop.reason_code == Reason.CONFIRMATION_REQUIRED
    (pending,) = h.snapshots.confirmations
    assert pending["key_token"] == KEY and pending["principal_id"] == "u1"
    assert pending["display"] == {"q": ("비밀" * 150)[:256]}
    events = json.dumps([e.payload for e in h.store.events.values()], ensure_ascii=False)
    assert "비밀" not in events
    assert not h.buffer.has_pending() and h.stderr.getvalue() == ""


@pytest.mark.parametrize("metadata", [None, {"service_account_id": "svc"}])
def test_nonhuman_is_blocked_without_a_pending_row(tmp_path, metadata):
    h = setup(tmp_path)
    result = asyncio.run(evaluate_request(request(user_principal(user_id=None, metadata=metadata)),
                                          h.deps))
    assert result.snapshot.stop.reason_code == Reason.CONFIRMATION_REQUIRED
    assert not h.snapshots.confirmations


@pytest.mark.parametrize(("override", "reason"), [
    ({"args_fp": "a" * 64}, Reason.CONFIRMATION_REQUIRED),
    ({"policy_version_id": "old"}, Reason.CONFIRMATION_STALE),
    ({"fp_key_version": 2}, Reason.CONFIRMATION_REQUIRED),
    ({"expires_at": NOW}, Reason.CONFIRMATION_REQUIRED),
    ({"org_id": "other"}, Reason.CONFIRMATION_REQUIRED),
    ({"confirmed_by_user_id": "other"}, Reason.CONFIRMATION_REQUIRED),
    ({"confirmed_by_key_token": KEY}, Reason.CONFIRMATION_REQUIRED),
])
def test_confirmation_matching_fails_closed(tmp_path, override, reason):
    h = setup(tmp_path, snapshot(self_confirmations=(confirmation(**override),)))
    result = asyncio.run(evaluate_request(request(), h.deps))
    assert not result.allowed and result.snapshot.stop.reason_code == reason
    assert len(h.snapshots.confirmations) == 1 and not h.store.consumed


def test_other_key_confirmation_allows_and_only_consumes_confirmation(tmp_path):
    sc = confirmation()
    h = setup(tmp_path, snapshot(self_confirmations=(sc,)))
    result = asyncio.run(evaluate_request(request(), h.deps))
    assert result.allowed and not h.snapshots.confirmations
    (consume,) = h.store.consumed
    assert consume.confirmation.id == sc.id and consume.call_approval_id is None
    assert result.snapshot.consumed_call_approval_id is None
    assert result.context["consumed_call_approval"] is None


def test_confirmation_alone_never_satisfies_administrator_call_approval(tmp_path):
    h = setup(tmp_path, snapshot(self_confirmations=(confirmation(),)))
    h.deps.evaluate = evaluator("RequireApproval", policy_ids=("R2",), obligations=(CONFIRM,))
    result = asyncio.run(evaluate_request(request(), h.deps))
    assert not result.allowed and result.snapshot.stop.reason_code == Reason.CALL_APPROVAL_REQUIRED
    assert not h.store.consumed


def test_both_consumptions_are_carried_together_and_loss_uses_confirmation_reason(tmp_path):
    h = setup(tmp_path, snapshot(
        self_confirmations=(confirmation(),), call_approvals=(call_approval(),),
    ))
    h.deps.evaluate = evaluator("RequireApproval", policy_ids=("R2",), obligations=(CONFIRM,))
    result = asyncio.run(evaluate_request(request(), h.deps))
    assert result.allowed
    (consume,) = h.store.consumed
    assert consume.call_approval_id is not None and consume.confirmation is not None

    async def lost(event, consume=None):
        if consume is not None:
            raise ConsumptionLost("CONFIRMATION_REQUIRED")
        return True

    h.deps.recorder._store.insert_event = lost
    result = asyncio.run(evaluate_request(request(), h.deps))
    assert not result.allowed and result.snapshot.stop.reason_code == Reason.CONFIRMATION_REQUIRED


def test_display_without_fields_has_only_keys_and_sizes():
    assert confirmation_display({"q": "한글", "n": 3}, ()) == {
        "keys": ["n", "q"], "sizes": {"q": 6, "n": 1},
    }
    assert confirmation_display({"n": {"x": "a" * 300}}, ("n", "missing"))["n"] == (
        json.dumps({"x": "a" * 300})[:256]
    )


def test_conflicting_confirmation_purposes_stop_and_fields_are_unioned():
    same = compose_obligations([CONFIRM, {**CONFIRM, "display_fields": ["amount"]}])
    assert same.obligations == (
        RequireUserConfirmation(purpose="전송", display_fields=("amount", "q")),
    )
    conflict = compose_obligations([CONFIRM, {**CONFIRM, "purpose": "other"}])
    assert conflict.failure.reason_code == Reason.UNSUPPORTED


def test_separate_key_setting_can_explicitly_disable_separation(tmp_path):
    snap = snapshot(self_confirmations=(confirmation(confirmed_by_key_token=KEY),))
    settings = replace(snap.settings, require_separate_confirm_key=False)
    h = setup(tmp_path, replace(snap, settings=settings))
    assert asyncio.run(evaluate_request(request(), h.deps)).allowed
