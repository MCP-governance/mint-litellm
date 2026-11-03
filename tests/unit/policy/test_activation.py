import asyncio
import json
import socket
from contextlib import asynccontextmanager

from mint_litellm.plugin.policy.bundle import PreparedBundle, activate_bundle


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


def test_activation_verifies_loaded_metadata_before_database_activation():
    async def run():
        bundle = PreparedBundle("version", "a" * 64, "b" * 64, b"bundle")
        conn = Connection(bundle)
        published = []

        async def publish(data):
            published.append(data)

        async def handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            metadata = {"policy_version_id": "version", "bundle_sha256": "a" * 64}
            body = json.dumps({"result": metadata}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "localhost", 0, family=socket.AF_INET)
        try:
            port = server.sockets[0].getsockname()[1]
            await activate_bundle(
                conn,
                instance_id="instance",
                actor="operator",
                bundle=bundle,
                publish=publish,
                opa_url=f"http://localhost:{port}",
                timeout_seconds=3,
            )
        finally:
            server.close()
            await server.wait_closed()
        assert published == [b"bundle"]
        assert any("superseded" in sql for sql in conn.statements)
        assert any("INSERT INTO mint.events" in sql for sql in conn.statements)

    asyncio.run(run())
