"""정책 엔진이 번들 하나를 적재한 상태의 응답을 돌려주는 로컬 서버(OPA 1.21 응답 형식)."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mint_litellm.plugin.policy.bundle import DATA_FILE, bundle_content_sha256, parse_data_json

RULES = "package mint\n\ndefault allow := false\n"
DATA = {"business_hours": {"tz": "UTC", "days": [1], "start": "09:00", "end": "18:00"}}


class FakeOpa:

    def __init__(self, rules=RULES, data=None, bundles=None):
        self.rules, self.data = rules, DATA if data is None else data
        self.bundles = (
            {"mint": {"etag": "1", "manifest": {"revision": "1", "roots": ["mint"]}}}
            if bundles is None
            else bundles
        )
        self.meta = {"policy_version_id": "v1", "bundle_sha256": expected_hash()}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = {
                    "/v1/data/system/bundles": {"result": outer.bundles},
                    "/v1/policies": {"result": [outer.module()]},
                    "/v1/data": {"result": outer.tree()},
                    "/v1/data/system": {
                        "result": {"bundles": outer.bundles, "version": outer.version()}
                    },
                    "/v1/data/mint/data": {"result": outer.data},
                    "/v1/data/mint/meta": {"result": outer.meta},
                }.get(self.path)
                payload = json.dumps(body if body is not None else {}).encode()
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("localhost", 0), Handler)
        self.url = f"http://{socket.gethostbyname('localhost')}:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def version():
        keys = ("build_commit", "build_hostname", "build_timestamp", "version")
        return dict.fromkeys(keys, "x") | {"version": "1.21.4"}

    def module(self):
        package = [{"type": "var", "value": "data"}, {"type": "string", "value": "mint"}]
        head = {"ref": [{"type": "var", "value": "allow"}]}
        return {"id": "mint/policy.rego", "raw": self.rules,
                "ast": {"package": {"path": package}, "rules": [{"head": head}]}}

    def tree(self):
        return {"mint": {"allow": False, "data": self.data, "meta": self.meta}}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def expected_hash(rules=RULES, data=None):
    document = parse_data_json(json.dumps(DATA if data is None else data))
    return bundle_content_sha256({"policy.rego": rules}, {DATA_FILE: document})
