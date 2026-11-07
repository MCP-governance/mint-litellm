"""계수 역프록시 접근 기록(/__log)에서 서버별 tools/call 누적 수를 JSON으로 내는 작은 서버(표준 라이브러리).

GET /<서버 이름> -> {"tools/call": n}. 사용: counter_service.py <로그 주소> <포트>
"""
import json
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:4320/__log"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.strip("/")
        count = 0
        with urllib.request.urlopen(LOG, timeout=10) as response:
            for line in response.read().decode("utf-8", "replace").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("rpc") == "tools/call" and row.get("path", "").startswith(f"/{name}/"):
                    count += 1
        body = json.dumps({"tools/call": count}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2]) if len(sys.argv) > 2 else 4350), Handler).serve_forever()
