"""7. URL 선행 차단, 검사 상한, 직무 분리와 증거 만료."""

import pytest


@pytest.mark.parametrize("case", ["provider_denied", "private_literal", "allow_all_keys",
                                     "definitions_limit", "same_requester", "stale_evidence",
                                     "credentials_required"])
def test_negative_intake(rig, case):
    rig.require("intake create", "intake approve", "intake except", handlers=("intake_run",))
    rig.fault("intake_" + case)
    observation = rig.read_observation("intake_" + case)
    rig.check_observation("intake_" + case, observation)
    intake = rig.rows("intakes", "id=%s", [observation["intake_id"]])[0]
    if case in ("provider_denied", "private_literal", "allow_all_keys"):
        assert intake["decision"] == "rejected"
        assert rig.rows("intake_checks", "intake_id=%s AND result='fail'", [intake["id"]])
    if case == "credentials_required":
        assert intake["needs_registry_credentials"] is True
        checks = rig.rows("intake_checks", "intake_id=%s AND reason_code='CREDENTIALS_REQUIRED'",
                          [intake["id"]])
        assert checks
    if case in ("same_requester", "stale_evidence"):
        code = "SEPARATION-OF-DUTIES" if case == "same_requester" else "STALE-EVIDENCE"
        reply = rig.versioned(f"/intakes/{intake['id']}", "decision", {
            "decision": "approved", "reason": "acceptance",
        }, status=(403, 409))
        assert reply["code"] == "MINT-ADMIN-" + code
