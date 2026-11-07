"""pj1/pj3에서 실제 Claude Code를 실행하고 원문과 해시를 저장한다.

키는 환경변수를 참조하는 기존 Claude MCP 설정 파일에만 둔다.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=("pj1", "pj3"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--sha", required=True, help="솔루션 기기의 병합 main SHA")
    parser.add_argument("--server", choices=("playwright", "context7", "grafana"), required=True)
    parser.add_argument("--counter-url", required=True, help="이 서버의 격리된 계수 면")
    parser.add_argument("--arguments", type=Path, required=True)
    parser.add_argument("--phase", choices=("before", "active", "drift", "reapproved", "revoked"),
                        required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    executable = shutil.which("claude")
    assert executable, "Claude Code CLI가 필요합니다"
    arguments = json.loads(args.arguments.read_text(encoding="utf-8"))
    prompt = (
        f"MCP 도구 {args.tool}을 다음 인자로 정확히 한 번 호출하고 결과를 보고하세요. "
        f"재시도하지 마세요. 인자: {json.dumps(arguments, ensure_ascii=False)}"
    )
    def count():
        with urlopen(args.counter_url, timeout=10) as reply:
            return json.load(reply)["tools/call"]

    before = count()
    done = subprocess.run(
        [executable, "-p", prompt, "--strict-mcp-config", "--mcp-config", str(args.config),
         "--output-format", "json", "--allowedTools", args.tool],
        text=True, encoding="utf-8", capture_output=True, timeout=180,
    )
    time.sleep(2)
    delta = count() - before
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # 원문에는 클라이언트 오류가 있을 수 있다. 장비 로컬에만 보관하고 공유 전에 비밀 검색한다.
    raw = args.output.with_suffix(".log")
    raw.write_text(done.stdout + "\n" + done.stderr, encoding="utf-8", newline="\n")
    record = {
        "device": args.device, "client": "claude-code", "phase": args.phase,
        "exit_code": done.returncode, "tool": args.tool, "sha": args.sha,
        "upstream_calls": delta, "server": args.server,
        "transcript": {"path": str(raw), "sha256": hashlib.sha256(raw.read_bytes()).hexdigest()},
    }
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8", newline="\n")
    summary_path = args.output.parent / ("client-" + args.device + ".json")
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {
        "device": args.device, "client": "claude-code", "sha": args.sha, "servers": {},
        "phases": [],
    }
    assert summary["sha"] == args.sha, "이전 SHA의 클라이언트 증거는 새 디렉터리로 분리"
    summary["servers"].setdefault(args.server, {})[args.phase + "_upstream"] = delta
    summary["phases"].append(record)
    combined = args.output.parent / ("client-" + args.device + ".log")
    with combined.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.write(raw.read_text(encoding="utf-8"))
        stream.write("\n")
    summary["transcript"] = {
        "path": str(combined), "sha256": hashlib.sha256(combined.read_bytes()).hexdigest(),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8", newline="\n")
    assert done.returncode == 0, "Claude 실행 실패: 로컬 원문 확인"
    assert delta == (1 if args.phase in ("active", "reapproved") else 0), record


if __name__ == "__main__":
    main()
