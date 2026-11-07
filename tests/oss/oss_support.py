"""공개 MCP 서버 도입 시험 공용 도구: 스택 제어(stack.sh), LiteLLM 관리 API 호출, 서버 표."""

import json
import os
import re
import shlex
import subprocess
import uuid
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
LITELLM_URL = os.environ.get("MINT_OSS_LITELLM_URL", "http://localhost:4741")
GRAFANA_URL = os.environ.get("MINT_OSS_GRAFANA_URL", "http://localhost:4742")

# 시험 서버 표. url은 LiteLLM 컨테이너가 보는 compose 네트워크 안 주소다.
SERVERS = {
    "playwright": {
        "alias": "oss_playwright",
        "url": "http://mcp-playwright:8931/mcp",
        "port": 4743,
    },
    "context7": {"alias": "oss_context7", "url": "http://mcp-context7:3000/mcp", "port": 4744},
    "grafana": {"alias": "oss_grafana", "url": "http://mcp-grafana:8000/mcp", "port": 4745},
}
VERSIONS = {
    "playwright": ("0.0.83", "0.0.84"),
    "context7": ("2.2.1", "2.2.2"),
    "grafana": ("1.4.0", "1.4.1"),
}


def _stack_command() -> list[str]:
    """stack.sh를 부르는 명령. 도커가 WSL에만 있으면 MINT_OSS_WSL_DISTRO로 배포판을 준다."""
    distro = os.environ.get("MINT_OSS_WSL_DISTRO")
    script = HERE / "stack.sh"
    if not distro:
        return ["bash", str(script)]
    drive, rest = script.drive.rstrip(":").lower(), script.as_posix()[2:]
    return ["wsl", "-d", distro, "--", "bash", f"/mnt/{drive}{rest}"]


def stack(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    return subprocess.run(
        [*_stack_command(), *args],
        env=env,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=900,
    )


def runtime_env() -> dict[str, str]:
    """stack.sh up이 만든 실행 환경 파일. 없으면 빈 사전."""
    path = HERE / ".runtime" / "env"
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, raw = line.partition("=")
        if name:
            values[name] = shlex.split(raw)[0] if raw else ""
    return values


class Litellm:
    """LiteLLM 관리 API 클라이언트. 키가 없으면 마스터 키를 쓴다."""

    def __init__(self, key: str | None = None, base: str = LITELLM_URL):
        self.key = key or runtime_env().get("G10_MASTER_KEY", "")
        self.http = httpx.Client(base_url=base, timeout=60)

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.key}"}
        return self.http.request(method, path, headers=headers, **kw)

    def ready(self) -> bool:
        try:
            return self.http.get("/health/readiness", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def new_user_key(self, role: str = "internal_user") -> tuple[str, str]:
        """비관리자 사용자와 팀 범위 키를 만든다(제출 API는 팀 키를 요구한다). (user_id, key)."""
        user_id = f"oss-{uuid.uuid4().hex[:8]}"
        self.call(
            "POST", "/user/new", json={"user_id": user_id, "user_role": role}
        ).raise_for_status()
        team = self.call("POST", "/team/new", json={"team_alias": f"team-{user_id}"})
        team.raise_for_status()
        team_id = team.json()["team_id"]
        member = {"team_id": team_id, "member": {"user_id": user_id, "role": "user"}}
        self.call("POST", "/team/member_add", json=member).raise_for_status()
        key = self.call("POST", "/key/generate", json={"user_id": user_id, "team_id": team_id})
        key.raise_for_status()
        return user_id, key.json()["key"]

    def spec(self, server: str, **extra) -> dict:
        info = SERVERS[server]
        body = {
            "alias": info["alias"],
            "server_name": info["alias"],
            "url": info["url"],
            "transport": "http",
            "auth_type": "none",
            "allow_all_keys": False,
        }
        return {**body, **extra}


def _hint(tool: dict, name: str):
    return (tool.get("annotations") or {}).get(name)


def summarize_tools(tools: list[dict]) -> dict:
    """tools/list 응답의 크기·annotations 요약. 값이 null인 annotations 키는 없는 것으로 센다."""
    sizes = [len(json.dumps(t, ensure_ascii=False)) for t in tools]
    has = lambda t: any(v is not None for v in (t.get("annotations") or {}).values())  # noqa: E731
    return {
        "tools": len(tools),
        "bytes": sum(sizes),
        "max_tool_bytes": max(sizes, default=0),
        "with_annotations": sum(1 for t in tools if has(t)),
        "with_readOnlyHint": sum(1 for t in tools if _hint(t, "readOnlyHint") is not None),
        "readOnlyHint_true": sum(1 for t in tools if _hint(t, "readOnlyHint") is True),
        "with_destructiveHint": sum(1 for t in tools if _hint(t, "destructiveHint") is not None),
        "destructiveHint_true": sum(1 for t in tools if _hint(t, "destructiveHint") is True),
        "with_output_schema": sum(1 for t in tools if t.get("outputSchema")),
        "names": sorted(t["name"] for t in tools),
    }


def tool_signature(tool: dict) -> str:
    """도구 하나의 입력 스키마 정규형 문자열(드리프트 비교용)."""
    return json.dumps(tool.get("inputSchema") or tool.get("input_schema") or {}, sort_keys=True)


def redact(text: str) -> str:
    """로그·보고에 남길 문자열에서 키·토큰 모양을 가린다."""
    return re.sub(
        r"(sk-[A-Za-z0-9_-]{6,}|glsa_[A-Za-z0-9_]+|Bearer [A-Za-z0-9._-]+)", "<redacted>", text
    )
