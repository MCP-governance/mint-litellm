"""공식 MCP SDK(FastMCP, Streamable HTTP)로 만든 통합 시험용 업스트림 서버.

제품 배포·승인 목록 밖에서 업스트림 호출 수를 세고 결정론적 장애를 주입하는 데만 쓴다.
MCP 포트(MCP_PORT)와 제어 포트(CONTROL_PORT)는 분리되어 있고, 제어 포트만 시험 호스트에 열린다.

계수는 세 지점에서 따로 한다.
  arrived    HTTP 본문을 읽어 tools/call 메시지를 확인한 시점(SDK에 넘기기 전)
  entered    SDK의 CallToolRequest 처리기에 들어온 시점
  completed  처리기가 결과를 돌려준 시점
"""

import asyncio
import json
import os
import weakref
from typing import Any

import uvicorn
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import McpError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

INSTANCE = os.environ.get("INSTANCE_ID", "testserver")
MCP_PORT = int(os.environ.get("MCP_PORT", "9000"))
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "9001"))

MUTABLE_VARIANTS = {
    "v1": "Returns the stored value (variant one).",
    "v2": "Returns the stored value repeated count times (variant two).",
}


class State:
    def __init__(self) -> None:
        self.sessions: weakref.WeakSet[Any] = weakref.WeakSet()
        self.reset()

    def reset(self) -> None:
        self.arrived_by_method: dict[str, int] = {}
        self.tools_call: dict[str, dict[str, int]] = {}
        self.list_changed_sent = 0
        self.faults: dict[str, Any] = {}
        self.variant = "v1"
        self.description = MUTABLE_VARIANTS["v1"]

    def bump(self, marker: str, field: str) -> None:
        row = self.tools_call.setdefault(marker, {"arrived": 0, "entered": 0, "completed": 0})
        row[field] += 1

    def totals(self) -> dict[str, int]:
        out = {"arrived": 0, "entered": 0, "completed": 0}
        for row in self.tools_call.values():
            for key, value in row.items():
                out[key] += value
        return out


state = State()


def rpc_messages(body: bytes) -> list[tuple[str, str]]:
    """본문의 JSON-RPC 메시지를 (method, marker) 목록으로 푼다. 해석할 수 없으면 빈 목록."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return []
    out: list[tuple[str, str]] = []
    for message in parsed if isinstance(parsed, list) else [parsed]:
        if not isinstance(message, dict) or "method" not in message:
            continue
        params = message.get("params") or {}
        arguments = params.get("arguments") if isinstance(params, dict) else None
        marker = arguments.get("marker", "") if isinstance(arguments, dict) else ""
        out.append((str(message["method"]), str(marker)))
    return out


class Gate:
    """SDK 앞단에서 도착을 세고 HTTP 상태 코드·응답 중간 끊김 장애를 주입한다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        chunks, more = [], True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)
        calls = []
        for method, marker in rpc_messages(body):
            state.arrived_by_method[method] = state.arrived_by_method.get(method, 0) + 1
            if method == "tools/call":
                state.bump(marker, "arrived")
                calls.append(marker)

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        faults = state.faults
        if calls and faults.get("http_status"):
            await send(
                {
                    "type": "http.response.start",
                    "status": int(faults["http_status"]),
                    "headers": [(b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        cut = faults.get("cut") if calls else None
        if cut:
            if cut == "after_exec":

                async def discard(message):
                    pass

                await self.app(scope, replay, discard)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", b"4096"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"jsonrpc"', "more_body": True})
            # 응답 시작 뒤 예외를 던지면 uvicorn이 연결을 닫아 클라이언트가 중간 끊김을 본다.
            raise ConnectionAbortedError("injected cut")
        await self.app(scope, replay, send)


def register_mutable(server: FastMCP) -> None:
    if state.variant == "v1":

        def mutable(text: str, marker: str = "") -> str:
            return text
    else:

        def mutable(text: str, count: int, marker: str = "") -> str:
            return text * count

    server.add_tool(mutable, name="mutable", description=state.description)


def build() -> FastMCP:
    server = FastMCP(
        INSTANCE,
        host="",
        port=MCP_PORT,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool(description="Returns the given text together with the serving instance name.")
    def echo(text: str, marker: str = "") -> str:
        return f"{INSTANCE}:{text}"

    @server.tool(description="Waits for the given number of seconds, then returns.")
    async def slow(seconds: float, marker: str = "") -> str:
        await asyncio.sleep(seconds)
        return f"{INSTANCE}:slept"

    @server.prompt(description="A fixed greeting prompt.")
    def greeting() -> str:
        return "hello"

    @server.resource("note://hello", description="A fixed text resource.")
    def note() -> str:
        return "hello"

    register_mutable(server)

    low = server._mcp_server
    inner_call = low.request_handlers[types.CallToolRequest]
    inner_list = low.request_handlers[types.ListToolsRequest]

    async def counted_call(req):
        state.sessions.add(low.request_context.session)
        marker = str((req.params.arguments or {}).get("marker", ""))
        state.bump(marker, "entered")
        faults = state.faults
        if faults.get("delay_ms"):
            await asyncio.sleep(int(faults["delay_ms"]) / 1000)
        if faults.get("rpc_error"):
            raise McpError(types.ErrorData(code=-32001, message="injected rpc error"))
        if faults.get("tool_error"):
            state.bump(marker, "completed")
            return types.ServerResult(
                types.CallToolResult(
                    content=[types.TextContent(type="text", text="injected tool error")],
                    isError=True,
                )
            )
        result = await inner_call(req)
        state.bump(marker, "completed")
        return result

    async def listed(req):
        state.sessions.add(low.request_context.session)
        return await inner_list(req)

    low.request_handlers[types.CallToolRequest] = counted_call
    low.request_handlers[types.ListToolsRequest] = listed
    return server


async def notify_list_changed() -> int:
    sent = 0
    for session in list(state.sessions):
        try:
            await session.send_tool_list_changed()
            sent += 1
        except Exception:
            continue
    state.list_changed_sent += sent
    return sent


def control_app(server: FastMCP) -> Starlette:
    async def health(_: Request):
        return JSONResponse({"ok": True, "instance": INSTANCE})

    async def counters(_: Request):
        return JSONResponse(
            {
                "instance": INSTANCE,
                "tools_call": state.totals(),
                "by_marker": state.tools_call,
                "by_method": state.arrived_by_method,
                "list_changed_sent": state.list_changed_sent,
                "variant": state.variant,
                "faults": state.faults,
            }
        )

    async def set_faults(request: Request):
        state.faults = await request.json() if request.method == "POST" else {}
        return JSONResponse(state.faults)

    async def reset(_: Request):
        state.reset()
        server.remove_tool("mutable")
        register_mutable(server)
        return JSONResponse({"ok": True})

    async def mutate(request: Request):
        body = await request.json()
        variant = body.get("variant", state.variant)
        if variant not in MUTABLE_VARIANTS:
            return JSONResponse({"error": "unknown variant"}, status_code=400)
        state.variant = variant
        state.description = body.get("description", MUTABLE_VARIANTS[variant])
        server.remove_tool("mutable")
        register_mutable(server)
        sent = await notify_list_changed()
        return JSONResponse({"variant": state.variant, "notified_sessions": sent})

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/counters", counters),
            Route("/faults", set_faults, methods=["POST", "DELETE"]),
            Route("/reset", reset, methods=["POST"]),
            Route("/mutate", mutate, methods=["POST"]),
        ]
    )


async def main() -> None:
    server = build()
    mcp_app = Gate(server.streamable_http_app())
    servers = [
        uvicorn.Server(uvicorn.Config(mcp_app, host="", port=MCP_PORT, log_level="warning")),
        uvicorn.Server(
            uvicorn.Config(
                control_app(server), host="", port=CONTROL_PORT, log_level="warning"
            )
        ),
    ]
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
