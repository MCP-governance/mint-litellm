"""``PluginDatabase._run``: 기다리기를 그만두면 연결을 끊고 빌리는 중이면 취소한다(DB 없이)."""

import asyncio
import contextlib

import pytest

from mint_litellm.plugin import snapshot as snapshot_module
from mint_litellm.plugin.snapshot import PluginDatabase


class FakeConnection:
    def __init__(self):
        self.pgconn = object()


def database_with(connection_context):
    db = PluginDatabase("postgresql://unused")

    async def connection():
        return connection_context()

    db._connection = connection
    return db


def run(coro):
    return asyncio.run(coro)


def test_a_result_and_an_error_pass_through_unchanged():
    @contextlib.asynccontextmanager
    async def connection():
        yield FakeConnection()

    db = database_with(connection)

    async def go():
        async def fine(conn):
            return 7

        async def broken(conn):
            raise KeyError("x")

        assert await db._run(fine) == 7
        with pytest.raises(KeyError):
            await db._run(broken)

    run(go())


def test_giving_up_on_a_borrowed_connection_severs_it_and_lets_the_work_fail_by_itself(
    monkeypatch,
):
    severed = []
    finished = asyncio.Event
    state = {}

    @contextlib.asynccontextmanager
    async def connection():
        conn = FakeConnection()
        state["conn"] = conn
        try:
            yield conn
        finally:
            state["released"] = True

    monkeypatch.setattr(snapshot_module, "_sever", lambda conn: severed.append(conn))
    db = database_with(connection)

    async def go():
        gate = finished()

        async def hangs_until_severed(conn):
            state["entered"] = True
            while not severed:
                await asyncio.sleep(0.01)
            gate.set()
            raise ConnectionError("severed")

        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                await db._run(hangs_until_severed)
        returned = asyncio.get_running_loop().time() - started
        await asyncio.wait_for(gate.wait(), 2)
        await asyncio.sleep(0.05)
        return returned

    returned = run(go())
    assert returned < 0.5
    assert severed == [state["conn"]] and state["released"] is True


def test_giving_up_before_a_connection_is_borrowed_cancels_the_borrowing():
    state = {"cancelled": False}

    class Slow:
        async def __aenter__(self):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        async def __aexit__(self, *exc):
            return False

    db = database_with(Slow)

    async def go():
        async def work(conn):
            return 1

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await db._run(work)
        await asyncio.sleep(0.05)

    run(go())
    assert state["cancelled"] is True
