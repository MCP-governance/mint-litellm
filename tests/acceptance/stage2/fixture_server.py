"""격리된 시험 네트워크의 합성 MCP/계수 서버. 표준 라이브러리만 사용한다.

공개 OSS 세 서버를 대체하지 않는다. 설명 변경·추가·삭제·64KB 인자에만 쓴다.
"""

import argparse
import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.mode = "base"
        self.counts = Counter()

    def tools(self):
        tools = [{
            "name": name, "description": "Returns test metadata only.",
            "inputSchema": {"type": "object", "additionalProperties": True},
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        } for name in ("mutable", "stable", "large")]
        if self.mode == "description":
            tools[0]["description"] = "Changed description for acceptance."
        elif self.mode == "schema":
            tools[0]["inputSchema"]["properties"] = {"count": {"type": "integer"}}
        elif self.mode == "added":
            tools.append({**tools[1], "name": "added"})
        elif self.mode == "removed":
            tools = tools[1:]
        elif self.mode == "limit":
            tools = [{**tools[1], "name": f"tool_{i}"} for i in range(501)]
        return tools


STATE = State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, status, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/counters":
            with STATE.lock:
                self.send_json(200, {"tools/call": 0, **STATE.counts})
        elif self.path == "/health":
            self.send_json(200, {"ready": True})
        else:
            self.send_json(405, {"error": "JSON response transport only"})

    def do_DELETE(self):
        self.send_json(200, {})

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 4 * 1024 * 1024:
            self.send_json(413, {"error": "fixture request too large"})
            return
        try:
            request = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeError):
            self.send_json(400, {"error": "invalid JSON"})
            return
        with STATE.lock:
            if self.path == "/control":
                mode = request["mode"]
                if mode not in {"base", "description", "schema", "added", "removed", "limit"}:
                    self.send_json(400, {"error": "unknown mode"})
                    return
                STATE.mode = mode
                self.send_json(200, {"mode": STATE.mode})
                return
            method = request.get("method")
            STATE.counts[method] += 1
            if "id" not in request:
                self.send_json(202, {})
                return
            if method == "initialize":
                result = {
                    "protocolVersion": request["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "acceptance-fixture", "version": "1"},
                }
            elif method == "tools/list":
                result = {"tools": STATE.tools()}
            elif method == "tools/call":
                params = request["params"]
                if params["name"] not in {tool["name"] for tool in STATE.tools()}:
                    self.send_json(200, {"jsonrpc": "2.0", "id": request["id"],
                                         "error": {"code": -32602, "message": "unknown tool"}})
                    return
                result = {"content": [{"type": "text", "text": "accepted"}], "isError": False}
            elif method == "ping":
                result = {}
            else:
                self.send_json(200, {"jsonrpc": "2.0", "id": request["id"],
                                     "error": {"code": -32601, "message": "unknown method"}})
                return
            self.send_json(200, {"jsonrpc": "2.0", "id": request["id"], "result": result})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", required=True)
    parser.add_argument("--port", type=int, default=8940)
    options = parser.parse_args()
    ThreadingHTTPServer((options.bind, options.port), Handler).serve_forever()
