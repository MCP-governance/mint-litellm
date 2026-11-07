"""11. 제어 평면 중단 중 판정과 복구 뒤 작업 재개."""


def test_control_plane_outage(rig):
    rig.require("console", "worker", handlers=("contract_observe", "sync_target"))
    fixture = rig.active()
    # 재관측 직후 시작해 두 관측 기한을 넘기기 전에 호출을 끝낸다.
    rig.wait(lambda: rig.rows("servers", "server_id=%s AND last_observed_at > "
                              "clock_timestamp() - interval '10 seconds'",
                              [fixture["server_id"]]), 90, "최근 관측")
    rig.fault("console_stop")
    rig.fault("worker_stop")
    for base in rig.site["litellm"]:
        rig.call(fixture["server_id"], fixture["tool"], fixture["arguments"], base=base)
    rig.restore_fault("worker_stop")
    rig.restore_fault("console_stop")
    rig.wait(lambda: rig.rows("jobs", "status='done' AND updated_at > clock_timestamp() "
                              "- interval '10 seconds'"), 90, "작업 재개")
    rig.note(control_plane_outage=True, replicas=len(rig.site["litellm"]))
