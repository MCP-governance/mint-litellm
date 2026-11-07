"""4. 소유 권한 재조회·보류·철회와 경쟁/임대 회수."""

import pytest


def test_sync_hold_resume_revoke(rig):
    rig.require("sync status", "sync resume", handlers=("sync_target", "sync_reconcile",))
    fixture = rig.active()
    server = fixture["server_id"]
    grants = rig.rows("litellm_grants", "server_id=%s AND NOT preexisting AND item='mcp_server'",
                      [server])
    assert grants
    grant = grants[0]
    rig.fault("delete_owned_grant", restore=False)
    rig.wait(lambda: rig.rows("litellm_grants", "id=%s AND observed AND missing_count=1",
                              [grant["id"]]),
             rig.expected["limits"]["sync_seconds"], "첫 권한 복구")
    window = rig.begin("sync_hold", entities=[grant["id"]])
    rig.fault("delete_owned_grant", restore=False)
    rig.wait(lambda: rig.rows("litellm_grants", "id=%s AND held", [grant["id"]]),
             rig.expected["limits"]["sync_seconds"], "두 번째 삭제 보류")
    rig.check_events(window)
    rig.cli("sync", "resume", str(grant["id"]))
    rig.wait(lambda: rig.rows("litellm_grants", "id=%s AND observed AND NOT held", [grant["id"]]),
             rig.expected["limits"]["sync_seconds"], "수동 재개")
    approval = rig.rows("approvals", "server_id=%s AND status='active'", [server])[0]
    window = rig.begin("revocation", entities=[approval["id"]])
    rig.cli("revoke", str(approval["id"]), "--reason", "acceptance")
    rig.wait(lambda: all(row["observed"] is False for row in rig.rows(
        "litellm_grants", "server_id=%s AND NOT preexisting AND item='mcp_server'", [server],
    )), 60, "철회 read-back")
    rig.call(server, fixture["tool"], fixture["arguments"], allowed=False,
             code="MINT-APPROVAL-REVOKED")
    rig.check_events(window)


@pytest.mark.parametrize("case", ["team_order", "preexisting", "pin_mismatch",
                                     "race_30", "dedup", "lease_reclaim", "revoke_race"])
def test_sync_edges(rig, case):
    rig.require("sync status", handlers=("sync_target", "sync_server"))
    fixture = rig.active("synthetic")
    rig.fault("sync_" + case)
    # 장비의 장애 주입 명령은 산출물에 관측값만 쓴다. 기대값은 공통 표의 고정값이다.
    observed = rig.read_observation("sync_" + case)
    rig.check_observation("sync_" + case, observed)
    assert observed["server_id"] == fixture["server_id"]
