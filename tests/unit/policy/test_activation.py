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
)

REGO = "package mint\n"


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


async def activate_against_fake_opa(served_rego, served_data):
    """등록된 해시는 ``REGO``와 ``{"k": 1}``의 것이고, 가짜 OPA는 인자로 받은 내용을 돌려준다."""
    digest = bundle_content_sha256({"mint/a.rego": REGO}, {DATA_FILE: {"k": 1}})
    bundle = PreparedBundle("version", digest, "b" * 64, b"bundle")
    conn = Connection(bundle)
    published = []

    async def publish(data):
        published.append(data)

    async def handler(reader, writer):
        request = (await reader.readuntil(b"\r\n\r\n")).split(b" ")[1].decode()
        replies = {
            "/v1/data/mint/meta": {"policy_version_id": "version", "bundle_sha256": digest},
            "/v1/data/system/bundles": {"/loaded/bundle.tar.gz": {}},
            "/v1/policies": [{"id": "/loaded/bundle.tar.gz/mint/a.rego", "raw": served_rego}],
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
            publish=publish, opa_url=f"http://{host}:{port}", timeout_seconds=3,
        )
    finally:
        server.close()
        await server.wait_closed()
    return published, conn.statements


def test_activation_verifies_loaded_content_before_database_activation():
    published, statements = asyncio.run(activate_against_fake_opa(REGO, {"k": 1}))
    assert published == [b"bundle"]
    assert any("superseded" in sql for sql in statements)
    assert any("INSERT INTO mint.events" in sql for sql in statements)


@pytest.mark.parametrize(
    ("rego", "data"), [(REGO + "# changed\n", {"k": 1}), (REGO, {"k": 2}), (REGO, {"k": 1, "x": 1})]
)
def test_activation_rejects_loaded_content_that_differs_from_the_registered_hash(rego, data):
    with pytest.raises(BundleContentMismatch):
        asyncio.run(activate_against_fake_opa(rego, data))
