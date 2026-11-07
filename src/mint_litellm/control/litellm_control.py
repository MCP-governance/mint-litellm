"""LiteLLM 제어 평면 클라이언트: 읽기에 더해 허용된 쓰기 경로만 부른다.

제어 평면 키는 ``proxy_admin``이고 ``allowed_routes``로 제한한다. LiteLLM은 갱신 필드를 제한하지
못하므로 이 클라이언트가 쓰기의 메서드·경로·본문을 직접 제한한다. 서버 등록·수정·삭제·가져오기
경로는 부르지 않고, 키·팀 갱신은 ``object_permission``의 두 필드만 보낸다. 모든 LiteLLM 쓰기는
``_write`` 한 곳을 지나며 그 직전에 ``guard``(되돌림 상태·임대 유효성 확인)를 부른다.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from mint_litellm.control.errors import ControlError
from mint_litellm.control.litellm_admin import LiteLLMAdmin, LiteLLMError, scrub

# 2단계가 제어 평면 키의 allowed_routes에 더하는 경로(1단계 목록에 이어 붙인다).
STAGE2_ALLOWED_ROUTES: tuple[str, ...] = (
    "/key/update",
    "/team/update",
    "/mcp-rest/test/connection",
    "/mcp-rest/test/tools/list",
)

_SERVER = r"/v1/mcp/server/[^/]+"
# 상태를 바꾸는 경로: (메서드, 경로 정규식)
WRITE_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("PUT", rf"{_SERVER}/approve"),
        ("PUT", rf"{_SERVER}/reject"),
        ("POST", rf"{_SERVER}/pin"),
        ("DELETE", rf"{_SERVER}/pin"),
        ("PUT", r"/v1/mcp/server"),  # allow_all_keys 끄기 전용(본문 제한)
        ("POST", r"/key/update"),
        ("POST", r"/team/update"),
    )
)
# 업스트림 서버에 접속해 보는 시험 경로(LiteLLM 상태를 바꾸지 않는다)
PROBE_ROUTES = frozenset({"/mcp-rest/test/connection", "/mcp-rest/test/tools/list"})
PROBE_FIELDS = frozenset({"url", "transport", "auth_type", "server_id"})
CLOSE_FIELDS = frozenset({"server_id", "allow_all_keys"})
PERMISSION_FIELDS = frozenset({"mcp_servers", "mcp_tool_permissions"})
MAX_TOOLS_BYTES = 2 * 1024 * 1024


class ResponseTooLarge(LiteLLMError):
    """응답이 상한을 넘었다."""


class LiteLLMControl(LiteLLMAdmin):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 35.0,
        guard: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(base_url, api_key, timeout)
        self._guard = guard

    def with_guard(self, guard: Callable[[], None]) -> "LiteLLMControl":
        """같은 접속으로 쓰기 직전 경계만 바꾼 사본."""
        return LiteLLMControl(self._base, self._key, self._timeout, guard)

    # --- 전송 -------------------------------------------------------------------

    def _send(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        max_bytes: int | None = None,
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"{self._base}{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                limit = None if max_bytes is None else max_bytes + 1
                status, raw = response.status, response.read(limit)
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise LiteLLMError(f"LiteLLM 관리 API에 연결할 수 없습니다: {error}") from None
        if max_bytes is not None and len(raw) > max_bytes:
            raise ResponseTooLarge(f"LiteLLM {path} 응답이 {max_bytes}바이트를 넘었습니다.")
        try:
            parsed = scrub(json.loads(raw)) if raw else None
        except ValueError:
            parsed = None
        return status, parsed

    def _write(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        """LiteLLM 쓰기의 단 하나의 통로. 허용된 (메서드, 경로)만 보내고 그 직전에 경계를 부른다."""
        if not any(m == method and p.fullmatch(path) for m, p in WRITE_ROUTES):
            raise ControlError(f"허용되지 않은 LiteLLM 쓰기입니다: {method} {path}")
        if method == "PUT" and path == "/v1/mcp/server" and (
            not isinstance(body, dict)
            or set(body) != CLOSE_FIELDS
            or body["allow_all_keys"] is not False
        ):
            raise ControlError("서버 수정은 allow_all_keys를 끄는 본문만 보낼 수 있습니다.")
        if self._guard is not None:
            self._guard()
        return self._send(method, path, body)

    # --- 읽기 -------------------------------------------------------------------

    def list_submissions(self) -> list[dict]:
        """제출 목록(``items``). 대기·승인·거절이 모두 들어 있으므로 호출자가 거른다."""
        body = self._ok("/v1/mcp/server/submissions", key="items", shape=list)
        return [item for item in body["items"] if isinstance(item, dict)]

    def _probe(self, path: str, spec: dict, max_bytes: int | None = None) -> dict:
        extra = set(spec) - PROBE_FIELDS
        if path not in PROBE_ROUTES or extra:
            raise ControlError(f"시험 요청에 허용되지 않은 필드입니다: {sorted(extra) or path}")
        status, body = self._send("POST", path, spec, max_bytes=max_bytes)
        if status in (401, 403):
            raise LiteLLMError(
                f"제어 평면 키로 {path}를 부를 수 없습니다(HTTP {status}). "
                "allowed_routes를 확인하십시오."
            )
        if status != 200 or not isinstance(body, dict):
            raise LiteLLMError(f"LiteLLM {path} 응답이 올바르지 않습니다(HTTP {status}).")
        return body

    def test_connection(self, spec: dict) -> dict:
        """``{status: "ok"}`` 또는 ``{status: "error", message}``. 연결 실패도 HTTP 200으로 온다."""
        return self._probe("/mcp-rest/test/connection", spec)

    def test_tools_list(self, spec: dict, max_bytes: int = MAX_TOOLS_BYTES) -> dict:
        """``{tools: [...]}`` 또는 ``{status: "error", message}``."""
        return self._probe("/mcp-rest/test/tools/list", spec, max_bytes)

    # --- 쓰기 -------------------------------------------------------------------

    @staticmethod
    def _server_path(server_id: str, action: str) -> str:
        return f"/v1/mcp/server/{urllib.parse.quote(server_id, safe='')}/{action}"

    def _review(self, server_id: str, action: str, body: dict | None, done_word: str) -> str:
        status, out = self._write("PUT", self._server_path(server_id, action), body)
        if status == 200:
            return "done"
        text = json.dumps(out, ensure_ascii=False) if out is not None else ""
        if status == 400 and f"already {done_word}" in text:
            return "already"
        raise LiteLLMError(f"LiteLLM 제출 {action} 응답이 올바르지 않습니다(HTTP {status}).")

    def approve_submission(self, server_id: str) -> str:
        """제출을 승인한다. 이미 승인됐으면 ``already``, 방금 승인했으면 ``done``."""
        return self._review(server_id, "approve", None, "active")

    def reject_submission(self, server_id: str, notes: str) -> str:
        return self._review(server_id, "reject", {"review_notes": notes[:500]}, "rejected")

    def close_all_keys(self, server_id: str) -> bool:
        """서버의 ``allow_all_keys``를 끄고 다시 읽어 확인한다. 꺼졌으면 참.

        ``PUT /v1/mcp/server``는 본문의 ``server_id``와 ``allow_all_keys``만 바꾸고 나머지
        (주소·인증·저장 자격)는 그대로 둔다(실측). 확인은 서버 상세 읽기로 한다.
        """
        status, _ = self._write(
            "PUT", "/v1/mcp/server", {"server_id": server_id, "allow_all_keys": False}
        )
        if status not in (200, 202):
            return False
        entry = self.get_server(server_id)
        return entry is not None and entry.get("allow_all_keys") is False

    def update_object_permission(
        self, target: str, target_id: str, object_permission: dict
    ) -> dict:
        """키·팀의 ``object_permission``을 통째로 바꾼다(본문은 대상과 이 값뿐이다)."""
        if target not in ("key", "team"):
            raise ControlError("대상은 key 또는 team이어야 합니다.")
        if set(object_permission) - PERMISSION_FIELDS:
            raise ControlError(
                "object_permission은 mcp_servers·mcp_tool_permissions만 보낼 수 있습니다: "
                + ", ".join(sorted(set(object_permission) - PERMISSION_FIELDS))
            )
        body = {"key" if target == "key" else "team_id": target_id,
                "object_permission": object_permission}
        status, out = self._write("POST", f"/{target}/update", body)
        if status != 200 or not isinstance(out, dict):
            raise LiteLLMError(f"LiteLLM /{target}/update 응답이 올바르지 않습니다(HTTP {status}).")
        return out

    def pin(self, server_id: str, *, unpin: bool = False) -> dict | None:
        status, out = self._write(
            "DELETE" if unpin else "POST", self._server_path(server_id, "pin")
        )
        if status != 200:
            raise LiteLLMError(f"LiteLLM pin 응답이 올바르지 않습니다(HTTP {status}).")
        return out if isinstance(out, dict) else None
