"""제어 평면 클라이언트 시험용 로컬 HTTP 서버: 제출 목록·시험 경로·제출 승인/거절·사용자 조회.

응답 형태는 LiteLLM 1.106.6 실측값이다(연결 실패도 HTTP 200 + ``status: error``, 승인·거절 반복은
HTTP 400 ``already ...``, 제출 목록은 ``items``).
"""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

CONTROL_KEY = "control-plane-key"

TOOL = {
    "name": "echo",
    "title": None,
    "description": "Returns the given text.",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    "outputSchema": None,
    "annotations": None,
}


def submission(server_id="sub-1", **fields):
    item = {
        "server_id": server_id,
        "server_name": server_id,
        "url": "http://up.test:9000/mcp",
        "transport": "http",
        "auth_type": "none",
        "issuer": None,
        "spec_path": None,
        "approval_status": "pending_review",
        "submitted_by": "staff-1",
        "allow_all_keys": False,
        "allowed_tools": [],
        "submitted_at": "2026-11-07T05:55:52.971000Z",
        "credentials": None,
    }
    item.update(fields)
    return item


class ControlStub:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object]] = []
        self.submissions: list[dict] = []
        self.connection = (200, {"status": "ok"})
        self.tools = (200, {"tools": [TOOL], "error": None, "message": "ok"})
        self.users = {
            "staff-1": {"user_id": "staff-1", "user_role": "internal_user"},
            "human-1": {"user_id": "human-1", "user_role": "internal_user"},
        }
        self.servers: dict[str, dict] = {}
        self.tools_raw: bytes | None = None
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status, body, raw=None):
                payload = raw if raw is not None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _handle(self):
                path = unquote(urlsplit(self.path).path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                stub.requests.append((self.command, path, body))
                if self.headers.get("Authorization") != f"Bearer {CONTROL_KEY}":
                    return self._send(401, {"detail": "bad key"})
                if self.command == "GET" and path == "/v1/mcp/server/submissions":
                    return self._send(
                        200, {"total": len(stub.submissions), "items": stub.submissions}
                    )
                if self.command == "GET" and path.startswith("/v1/mcp/server/"):
                    entry = stub.servers.get(path.rsplit("/", 1)[1])
                    return self._send(200, entry) if entry else self._send(404, {"detail": "no"})
                if self.command == "GET" and path == "/user/info":
                    uid = dict(p.split("=") for p in urlsplit(self.path).query.split("&")).get(
                        "user_id"
                    )
                    user = stub.users.get(uid)
                    return (
                        self._send(200, {"user_id": uid, "user_info": user})
                        if user
                        else self._send(404, {"detail": "no"})
                    )
                if self.command == "POST" and path == "/mcp-rest/test/connection":
                    return self._send(*stub.connection)
                if self.command == "POST" and path == "/mcp-rest/test/tools/list":
                    if stub.tools_raw is not None:
                        return self._send(200, None, stub.tools_raw)
                    return self._send(*stub.tools)
                parts = path.split("/")
                if self.command == "PUT" and len(parts) == 6 and parts[5] in ("approve", "reject"):
                    item = next((i for i in stub.submissions if i["server_id"] == parts[4]), None)
                    if item is None:
                        return self._send(404, {"detail": {"error": "not found"}})
                    state = "active" if parts[5] == "approve" else "rejected"
                    if item["approval_status"] == state:
                        return self._send(
                            400, {"detail": {"error": f"MCP server is already {state}."}}
                        )
                    item["approval_status"] = state
                    return self._send(200, item)
                return self._send(404, {"detail": "unknown"})

            do_GET = do_POST = do_PUT = do_DELETE = _handle  # noqa: N815

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("localhost", 0), Handler)
        self.url = f"http://{socket.gethostbyname('localhost')}:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.requests if m in ("PUT", "DELETE")]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
