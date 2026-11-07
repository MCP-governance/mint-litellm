"""계약 재승인의 pin 쓰기 접점. 시험 목록·제출 클라이언트와 분리한다."""

import json
import urllib.error
import urllib.parse
import urllib.request

from mint_litellm.control.errors import ControlError


class ContractPinClient:
    def __init__(self, base_url: str, api_key: str, *, before_write=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.before_write = before_write

    def _send(self, server_id: str, method: str) -> dict:
        if self.before_write is not None:
            self.before_write()
        path = urllib.parse.quote(server_id, safe="")
        request = urllib.request.Request(
            f"{self.base_url}/v1/mcp/server/{path}/pin",
            method=method,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ControlError("pin 응답이 상한을 넘었습니다.")
                body = json.loads(raw)
        except (urllib.error.URLError, OSError, ValueError):
            raise ControlError("계약 pin 요청에 실패했습니다.") from None
        if not isinstance(body, dict):
            raise ControlError("pin 응답 형식이 올바르지 않습니다.")
        return body

    def pin(self, server_id: str) -> dict:
        return self._send(server_id, "POST")

    def unpin(self, server_id: str) -> None:
        self._send(server_id, "DELETE")
