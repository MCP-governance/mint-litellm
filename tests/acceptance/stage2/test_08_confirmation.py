"""8. 본인 확인, 원자 소비, 역할·열 보호와 v001 격리."""

import pytest


@pytest.mark.parametrize("case", ["same_key", "other_key_once", "changed_args", "stale_policy",
                                     "concurrent", "both_atomic", "both_rollback",
                                     "limit_20", "expiry_10m", "team_no_row", "replica_unique",
                                     "plugin_insert", "plugin_confirm", "freeze_columns",
                                     "call_approval_only", "v001_isolation"])
def test_confirmation(rig, case):
    rig.require("console", handlers=("approval_expiry_scan",))
    fixture = rig.active("synthetic")
    rig.fault("confirmation_" + case)
    observation = rig.read_observation("confirmation_" + case)
    rig.check_observation("confirmation_" + case, observation)
    rows = rig.rows("self_confirmations", "server_id=%s", [fixture["server_id"]])
    assert all(row["display"] is None for row in rows if row["status"] in (
        "consumed", "rejected", "cancelled", "expired",
    ))
    if case in ("other_key_once", "concurrent", "replica_unique"):
        assert sum(row["status"] == "consumed" for row in rows) == 1
    if case in ("both_atomic", "both_rollback"):
        consumed = observation["invocation_id"]
        counts = [len(rig.rows(table, "consumed_invocation_id=%s", [consumed]))
                  for table in ("call_approvals", "self_confirmations")]
        assert counts == ([1, 1] if case == "both_atomic" else [0, 0])
