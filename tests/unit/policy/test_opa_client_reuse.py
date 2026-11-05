import asyncio
import json
import time

import pytest

from mint_litellm.plugin.policy.opa_client import OPAClient
from mint_litellm.types.reason_codes import Reason

HASH = "a" * 64
INPUT = {"tool": {"name": "example"}}
OUTPUT = {
    "decision": "Allow",
    "policy_ids": [],
    "policy_version_id": "active",
    "bundle_sha256": HASH,
    "obligations": [],
    "observable": False,
    "missing_inputs": [],
}


async def keepalive_server(delay=0.0):
    """연결 수를 세는 keep-alive 서버."""
    state = {"connections": 0, "requests": 0}

    async def handler(reader, writer):
        state["connections"] += 1
        try:
            while True:
                headers = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                await reader.readexactly(length)
                state["requests"] += 1
                await asyncio.sleep(delay)
                body = json.dumps({"result": OUTPUT}).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handler, "localhost", 0, family=2)
    host, port = server.sockets[0].getsockname()[:2]
    return server, f"http://{host}:{port}", state


def test_connection_is_reused_across_calls():
    async def run():
        server, url, state = await keepalive_server()
        client = OPAClient(url, 2)
        try:
            for _ in range(3):
                result = await client.evaluate(
                    INPUT, policy_version_id="active", bundle_sha256=HASH
                )
                assert result.output is not None
        finally:
            await client.aclose()
            server.close()
            await server.wait_closed()
        return state

    state = asyncio.run(run())
    assert state == {"connections": 1, "requests": 3}


def test_each_call_uses_its_own_remaining_budget():
    async def run():
        server, url, _ = await keepalive_server(delay=1.0)
        client = OPAClient(url, 5)
        try:
            started = time.monotonic()
            short = await client.evaluate(
                INPUT, policy_version_id="active", bundle_sha256=HASH, budget_seconds=0.2
            )
            elapsed = time.monotonic() - started
            long = await client.evaluate(
                INPUT, policy_version_id="active", bundle_sha256=HASH, budget_seconds=3
            )
            exhausted = await client.evaluate(
                INPUT, policy_version_id="active", bundle_sha256=HASH, budget_seconds=0
            )
        finally:
            await client.aclose()
            server.close()
            await server.wait_closed()
        return short, elapsed, long, exhausted

    short, elapsed, long, exhausted = asyncio.run(run())
    assert short.failure.reason_code == Reason.DEPENDENCY_FAILURE and elapsed < 0.9
    assert long.output is not None
    assert exhausted.failure.reason_code == Reason.DEPENDENCY_FAILURE


def test_client_cannot_be_reused_on_another_event_loop():
    async def first(client):
        return await client.evaluate(INPUT, policy_version_id="active", bundle_sha256=HASH)

    async def run():
        server, url, _ = await keepalive_server()
        return server, OPAClient(url, 2)

    loop = asyncio.new_event_loop()
    try:
        server, client = loop.run_until_complete(run())
        assert loop.run_until_complete(first(client)).output is not None
        with pytest.raises(RuntimeError):
            asyncio.run(first(client))
        loop.run_until_complete(client.aclose())
        server.close()
        loop.run_until_complete(server.wait_closed())
    finally:
        loop.close()


def test_client_cannot_be_reused_after_fork(monkeypatch):
    async def run():
        server, url, _ = await keepalive_server()
        client = OPAClient(url, 2)
        try:
            await client.evaluate(INPUT, policy_version_id="active", bundle_sha256=HASH)
            monkeypatch.setattr("os.getpid", lambda: -1)
            with pytest.raises(RuntimeError):
                await client.evaluate(INPUT, policy_version_id="active", bundle_sha256=HASH)
            monkeypatch.undo()
        finally:
            await client.aclose()
            server.close()

    asyncio.run(run())
