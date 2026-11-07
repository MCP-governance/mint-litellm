"""pj1/pj3용 stdin 진입점. 고정된 원격 명령으로 실행하여 셸에 인자를 보간하지 않는다."""

import base64
import json
import os
import sys
from pathlib import Path

import client_probe


def main():
    payload = json.load(sys.stdin)
    root = Path(os.environ["ACC_CLIENT_EVIDENCE"]) / payload["batch_id"]
    root.mkdir(parents=True, exist_ok=True)
    arguments = root / (payload["server"] + "-arguments.json")
    arguments.write_text(json.dumps(payload["arguments"], ensure_ascii=False), encoding="utf-8")
    output = root / (payload["server"] + "-" + payload["phase"] + ".json")
    sys.argv = [
        "client_probe.py", "--device", payload["device"], "--sha", payload["sha"],
        "--server", payload["server"], "--phase", payload["phase"],
        "--config", os.environ["ACC_CLAUDE_CONFIG"], "--tool", payload["tool"],
        "--arguments", str(arguments), "--counter-url", payload["counter_url"],
        "--output", str(output),
    ]
    client_probe.main()
    summary = json.loads((root / ("client-" + payload["device"] + ".json")).read_text(
        encoding="utf-8",
    ))
    raw = Path(summary["transcript"]["path"]).read_bytes()
    print(json.dumps({"record": summary, "transcript_base64": base64.b64encode(raw).decode()}))


if __name__ == "__main__":
    main()
