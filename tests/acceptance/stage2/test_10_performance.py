"""10. 판정 구간 실측. RTT 차이는 보조값이며 합격 판정에 쓰지 않는다."""

from .latency import measure


def test_decision_latency(rig):
    rig.require("contract show", handlers=("contract_observe",))
    rig.require_module("mint_litellm.plugin.policy.patterns")
    fixture = rig.active("synthetic")
    report = measure(rig, fixture)
    rig.note(performance=report)
    for scenario in report["scenarios"]:
        for name, limit in rig.expected["limits"]["decision_ms"].items():
            assert scenario["decision_ms"][name] <= limit, scenario
