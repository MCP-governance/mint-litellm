"""통합 기반 자가 시험: 플러그인 없이 LiteLLM만 두고 계수 경로와 장애 스위치를 확인한다."""

import time

import pytest
import support
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from support import call_tool, marker, mcp_session, new_key, run


@pytest.fixture
def key(stack):
    return new_key()


def test_services_ready(stack):
    assert all(stack.values()), stack


def test_positive_control_then_zero(evidence):
    evidence.positive_control()
    mark = marker()
    evidence.assert_calls(mark, 0)


def test_allowed_call_counts_once(key, evidence):
    evidence.positive_control()
    mark = marker()
    result = run(call_tool(key, "a", "echo", {"text": "x", "marker": mark}))
    assert not result.isError
    evidence.assert_calls(mark, 1)


def test_second_replica_uses_same_counting_point(key, evidence):
    mark = marker()
    run(call_tool(key, "b", "echo", {"text": "x", "marker": mark}, port=support.LITELLM_PORTS[1]))
    evidence.assert_calls(mark, 1, server="b")


def test_rest_call_counts_once(key, evidence):
    mark = marker()
    reply = support.rest_call(key, "a", "echo", {"text": "x", "marker": mark})
    assert reply.status_code == 200, reply.text
    evidence.assert_calls(mark, 1)


def test_litellm_cannot_reach_test_server_directly(stack):
    code = (
        "import socket,sys\n"
        "try:\n socket.gethostbyname('testserver-a')\nexcept OSError:\n sys.exit(0)\nsys.exit(7)"
    )
    for service in ("litellm1", "litellm2"):
        done = support.compose("exec", "-T", service, "python", "-c", code, check=False)
        assert done.returncode == 0, (service, done.stdout, done.stderr)


def test_delay_switch(key, evidence, upstreams):
    upstreams["a"].set_faults(delay_ms=1500)
    mark = marker()
    started = time.monotonic()
    run(call_tool(key, "a", "echo", {"text": "x", "marker": mark}))
    assert time.monotonic() - started >= 1.5
    evidence.assert_calls(mark, 1)


def test_tool_error_switch(key, evidence, upstreams):
    upstreams["a"].set_faults(tool_error=True)
    mark = marker()
    result = run(call_tool(key, "a", "echo", {"text": "x", "marker": mark}))
    assert result.isError
    evidence.assert_calls(mark, 1)
    assert upstreams["a"].count(mark)["completed"] == 1


def test_rpc_error_switch(key, evidence, upstreams):
    upstreams["a"].set_faults(rpc_error=True)
    mark = marker()
    try:
        result = run(call_tool(key, "a", "echo", {"text": "x", "marker": mark}))
        assert result.isError
    except Exception:
        pass
    evidence.assert_calls(mark, 1)
    assert upstreams["a"].count(mark)["completed"] == 0


def test_http_status_switch_arrives_but_never_enters(key, proxy_log, upstreams):
    upstreams["a"].set_faults(http_status=503)
    mark = marker()
    try:
        result = run(call_tool(key, "a", "echo", {"text": "x", "marker": mark}))
        assert result.isError
    except Exception:
        pass
    time.sleep(1)
    row = upstreams["a"].count(mark)
    assert row["arrived"] >= 1 and row["entered"] == 0
    assert {r["status"] for r in proxy_log.tools_calls(mark, "a")} == {"503"}


def test_toxiproxy_reset_before_delivery(key, proxy_log, upstreams, toxics):
    toxics["a"].add("reset_peer", stream="upstream", timeout=0)
    mark = marker()
    try:
        run(call_tool(key, "a", "echo", {"text": "x", "marker": mark}))
    except Exception:
        pass
    time.sleep(1)
    row = upstreams["a"].count(mark)
    assert row["arrived"] == 0 and row["entered"] == 0


@pytest.mark.parametrize(("cut", "entered"), [("before_exec", 0), ("after_exec", 1)])
def test_server_cut_switch(key, upstreams, cut, entered):
    upstreams["a"].set_faults(cut=cut)
    mark = marker()
    try:
        run(call_tool(key, "a", "echo", {"text": "x", "marker": mark}))
    except Exception:
        pass
    time.sleep(1)
    row = upstreams["a"].count(mark)
    assert row["arrived"] >= 1 and row["entered"] == entered, row


def test_latency_toxic(key, evidence, toxics):
    toxics["a"].add("latency", stream="downstream", latency=800)
    mark = marker()
    started = time.monotonic()
    run(call_tool(key, "a", "echo", {"text": "x", "marker": mark}))
    assert time.monotonic() - started >= 0.8
    evidence.assert_calls(mark, 1)


def test_description_and_schema_change_with_list_changed(key, upstreams):
    def mutable(tools):
        return next(t for t in tools if t.name == "mcp_a-mutable")

    async def listing():
        async with mcp_session(key) as session:
            return (await session.list_tools()).tools

    before = mutable(run(listing()))
    assert "count" not in before.inputSchema["properties"]
    notified = {}

    async def direct():
        async with streamablehttp_client(support.url(support.PROXY_PORT, "/a/mcp")) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                await session.list_tools()
                notified.update(upstreams["a"].mutate("v2", "changed description"))
                await session.list_tools()

    run(direct())
    assert notified["notified_sessions"] >= 1
    assert upstreams["a"].counters()["list_changed_sent"] >= 1
    after = mutable(run(listing()))
    assert after.description == "changed description"
    assert "count" in after.inputSchema["properties"]


def test_counters_are_isolated_by_marker(key, evidence):
    first, second = marker(), marker()
    run(call_tool(key, "a", "echo", {"text": "x", "marker": first}))
    evidence.assert_calls(first, 1)
    evidence.assert_calls(second, 0)


def test_database_path_through_toxiproxy(stack, toxics):
    import psycopg

    through = support.mint_database_url().replace(
        str(support.POSTGRES_PORT), str(support.DB_PROXY_PORT)
    )
    with psycopg.connect(through, connect_timeout=3) as conn:
        assert conn.execute("select 1").fetchone() == (1,)
    toxics["postgres"].disable()
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(through, connect_timeout=3)
