import asyncio
import json
import socket
from contextlib import asynccontextmanager

import pytest

from mint_litellm.plugin.policy.bundle import (
    DATA_FILE,
    BundleContentMismatch,
    PreparedBundle,
    activate_bundle,
    bundle_content_sha256,
    parse_data_json,
)

REGO = "package mint\n"
STALE = REGO + "# stale\n"
META = {"policy_version_id": "version", "bundle_sha256": "ignored"}


class Cursor:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class Connection:
    def __init__(self, bundle):
        self.bundle = bundle
        self.statements = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql, parameters):
        self.statements.append(sql)
        if sql.startswith("SELECT catalog_sha256"):
            return Cursor((self.bundle.catalog_sha256, self.bundle.bundle_sha256, "validated"))
        if "RETURNING state_version" in sql:
            return Cursor((3,))
        return Cursor(None)


POLICY_AST = {
    "package": {"path": [{"type": "var", "value": "data"}, {"type": "string", "value": "mint"}]},
    "rules": [{"head": {"ref": [{"type": "var", "value": "x"}]}}],
}


async def activate_against_fake_opa(served_rego, served_data, served_tree=None, timeout=3):
    """등록된 해시는 ``REGO``와 ``{"k": 1}``의 것이고, 가짜 OPA는 인자로 받은 내용을 돌려준다.

    ``served_rego``는 호출마다 다른 내용을 돌려주는 함수일 수 있다.
    """
    data = {DATA_FILE: parse_data_json(json.dumps({"k": 1}))}
    digest = bundle_content_sha256({"mint/a.rego": REGO}, data)
    bundle = PreparedBundle("version", digest, "b" * 64, b"bundle")
    conn = Connection(bundle)
    published = []
    reads = []

    async def publish(data):
        published.append(data)

    async def handler(reader, writer):
        request = (await reader.readuntil(b"\r\n\r\n")).split(b" ")[1].decode()
        rego = served_rego() if callable(served_rego) and request == "/v1/policies" else served_rego
        if request == "/v1/policies":
            reads.append(rego)
        tree = served_tree or {
            "mint": {"x": 1, "data": served_data, "meta": {
                "policy_version_id": "version", "bundle_sha256": digest}}
        }
        replies = {
            "/v1/data": tree,
            "/v1/data/mint/meta": {"policy_version_id": "version", "bundle_sha256": digest},
            "/v1/data/system/bundles": {"/loaded/bundle.tar.gz": {}},
            "/v1/policies": [
                {"id": "/loaded/bundle.tar.gz/mint/a.rego", "raw": rego, "ast": POLICY_AST}
            ],
            "/v1/data/mint/data": served_data,
        }
        body = json.dumps({"result": replies[request]}).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "localhost", 0, family=socket.AF_INET)
    try:
        host, port = server.sockets[0].getsockname()[:2]
        await activate_bundle(
            conn, instance_id="instance", actor="operator", bundle=bundle,
            publish=publish, opa_url=f"http://{host}:{port}", timeout_seconds=timeout,
        )
    finally:
        server.close()
        await server.wait_closed()
    return published, conn.statements, reads


def test_activation_verifies_loaded_content_before_database_activation():
    published, statements, _ = asyncio.run(activate_against_fake_opa(REGO, {"k": 1}))
    assert published == [b"bundle"]
    assert any("superseded" in sql for sql in statements)
    assert any("INSERT INTO mint.events" in sql for sql in statements)


@pytest.mark.parametrize(
    ("rego", "data"), [(REGO + "# changed\n", {"k": 1}), (REGO, {"k": 2}), (REGO, {"k": 1, "x": 1})]
)
def test_activation_rejects_loaded_content_that_differs_from_the_registered_hash(rego, data):
    with pytest.raises(BundleContentMismatch):
        asyncio.run(activate_against_fake_opa(rego, data))


@pytest.mark.parametrize(
    "tree",
    [
        {"mint": {"x": 1, "data": {"k": 1}, "meta": META, "extra": {"k": 1}}, "root": 1},
        {"mint": {"x": 1, "data": {"k": 1}, "meta": META, "extra": 2}},
        {"mint": {"x": 1, "data": {"k": 1}, "meta": {**META, "more": 1}}},
        {"extra": 1, "mint": {"x": 1, "data": {"k": 1}, "meta": META}},
    ],
)
def test_activation_rejects_data_outside_the_bundle_contract(tree):
    with pytest.raises(BundleContentMismatch):
        asyncio.run(activate_against_fake_opa(REGO, {"k": 1}, tree, timeout=4))


def test_activation_waits_for_the_reload_when_the_metadata_is_already_current():
    stale = iter([STALE] * 3)

    def served():
        return next(stale, REGO)

    published, _, reads = asyncio.run(activate_against_fake_opa(served, {"k": 1}, timeout=5))
    assert published == [b"bundle"]
    assert reads[:3] == [STALE] * 3 and reads[3] == REGO
