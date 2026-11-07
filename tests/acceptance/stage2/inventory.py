"""장비 연결 설정의 누락을 DB/Docker 없이 열거한다. 준비 완료를 추정하지 않는다."""

import argparse
import json
from pathlib import Path

from .rig import load_expectations


def required_faults():
    expected = load_expectations()
    return sorted((set(expected["observations"]) - {"client_flow"}) | {
        "playwright_upgrade", "context7_upgrade", "grafana_upgrade",
        "contract_description", "contract_added", "contract_removed", "contract_deployment",
        "worker_stop", "console_stop", "delete_owned_grant", "export_unwritable",
        "trigger_negative_state", "seal_tamper",
    })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    site = json.loads(args.site.read_text(encoding="utf-8")) if args.site else {}
    required = required_faults()
    missing = sorted(set(required) - site.get("faults", {}).keys())
    report = {
        "required_faults": required, "missing_faults": missing,
        "case_setup_configured": bool(site.get("case_setup")),
        "ready_to_run": not missing and bool(site.get("case_setup")),
        "interpretation": "이 검사는 설정 존재만 확인하며 실제 장비 동작을 검증하지 않는다.",
    }
    raw = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(raw, encoding="utf-8", newline="\n")
    else:
        print(raw, end="")
    return 0 if report["ready_to_run"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
