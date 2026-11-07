"""3. 관측 기한 초과와 재개."""

import time


def test_unobserved(rig):
    rig.require("contract show", handlers=("contract_observe",))
    fixture = rig.active()
    rig.fault("worker_stop")
    time.sleep(2 * rig.expected["limits"]["observe_seconds"] + 2)
    rig.call(fixture["server_id"], fixture["tool"], fixture["arguments"], allowed=False,
             code="MINT-CONTRACT-DRIFT-unobserved")
    rig.restore_fault("worker_stop")
    rig.wait(lambda: rig.rows("servers", "server_id=%s AND last_observed_at > "
                            "clock_timestamp() - interval '60 seconds'", [fixture["server_id"]]),
             90, "재관측")
    rig.call(fixture["server_id"], fixture["tool"], fixture["arguments"])
