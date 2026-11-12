"""Persistent MCP Controller and a schema-free stdio forwarding bridge."""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
from pathlib import Path

import anyio
import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .gateway import DESCRIPTIONS, MAX_OUTPUT, READ_ONLY, SCHEMAS, Gateway
from .service_state import load_registration


INSTRUCTIONS = (
    "Lightweight DeepSeek task distribution: submit → wait → resume paused work if needed → inspect delivered changes → Codex accepts or rejects. "
    "DELIVERED, ACK and process exit are not acceptance. request_id values are stable idempotency keys; "
    "after REQUEST_OUTCOME_UNKNOWN inspect the existing run instead of resubmitting. wait uses run.version and never cancels the run."
)


def _tool_list() -> types.ListToolsResult:
    return types.ListToolsResult(tools=[
        types.Tool(name=name, description=DESCRIPTIONS[name], inputSchema=schema,
                   annotations=types.ToolAnnotations(readOnlyHint=name in READ_ONLY,
                       destructiveHint=name not in READ_ONLY, idempotentHint=True, openWorldHint=False))
        for name, schema in SCHEMAS.items()
    ])


def make_server(gateway: Gateway) -> Server:
    async def list_tools(context, params):
        return _tool_list()

    async def call_tool(context, params):
        value = await anyio.to_thread.run_sync(gateway.handle, params.name, params.arguments or {})
        result = types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False, separators=(",", ":")))],
            isError=not value.get("ok", False),
        )
        if len(result.model_dump_json(by_alias=True, exclude_none=True).encode()) > MAX_OUTPUT - 1024:
            result = types.CallToolResult(
                content=[types.TextContent(type="text", text="Read the bounded result in structuredContent; reduce the page limit.")],
                structuredContent=value, isError=not value.get("ok", False),
            )
            if len(result.model_dump_json(by_alias=True, exclude_none=True).encode()) > MAX_OUTPUT - 1024:
                value = {"ok": False, "error": {"code": "OUTPUT_LIMIT", "request_id": value.get("request_id")}}
                result = types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(value))], isError=True)
        return result

    return Server("codex-deepseek-collaboration", version=__version__, instructions=INSTRUCTIONS,
                  on_list_tools=list_tools, on_call_tool=call_tool,
                  get_tool_input_schema=lambda name: SCHEMAS.get(name))


def make_http_app(gateway: Gateway, controller, registration, token, exit_callback):
    server = make_server(gateway)

    async def health(request):
        return JSONResponse({"ok": True, "result": controller.health()})

    async def stop(request):
        result = await anyio.to_thread.run_sync(controller.stop)
        return JSONResponse({"ok": True, "result": result},
                            background=BackgroundTask(exit_callback) if result.get("stop_requested") else None)

    app = server.streamable_http_app(
        streamable_http_path="/mcp", host="127.0.0.1", max_sessions=100,
        custom_starlette_routes=[Route("/v1/health", health, methods=["GET"]),
                                 Route("/v1/stop", stop, methods=["POST"])],
    )
    expected_host = f"127.0.0.1:{registration['port']}"
    expected_origin = "http://" + expected_host

    async def admission(request, call_next):
        if request.headers.get("host") != expected_host:
            return JSONResponse({"ok": False, "error": {"code": "INVALID_HOST"}}, status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and origin != expected_origin:
            return JSONResponse({"ok": False, "error": {"code": "INVALID_ORIGIN"}}, status_code=403)
        supplied = request.headers.get("authorization", "")
        if (not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode())
                or request.headers.get("x-collab-instance") != registration["instance_id"]):
            return JSONResponse({"ok": False, "error": {"code": "UNAUTHORIZED"}}, status_code=401)
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=admission)
    return app



async def _forward(state_root: Path, name, arguments):
    """One MCP call over a fresh loopback session (no cross-task connection state)."""
    registration, token = load_registration(state_root)
    headers = {"Authorization": "Bearer " + token,
               "X-Collab-Instance": registration["instance_id"]}
    client = httpx2.AsyncClient(headers=headers,
                                timeout=httpx2.Timeout(120.0, connect=5.0),
                                trust_env=False)
    async with client:
        async with streamable_http_client(registration["url"], http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(name, arguments or {})


async def serve_bridge(state_root: Path, execution_workspace: Path | None = None):
    async def list_tools(context, params):
        return _tool_list()

    async def call_tool(context, params):
        arguments = params.arguments or {}
        try:
            return await _forward(state_root, params.name, arguments)
        except Exception:
            if execution_workspace is not None and params.name != "collab_status":
                try:
                    from . import service
                    service.start(state_root, execution_workspace, native=True)
                except Exception:
                    pass
                else:
                    try:
                        return await _forward(state_root, params.name, arguments)
                    except Exception:
                        pass
            value = {"ok": False, "error": {"code": "SERVICE_UNAVAILABLE",
                                            "request_id": arguments.get("request_id")}}
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
                isError=True,
            )

    server = Server("codex-deepseek-collaboration", version=__version__, instructions=INSTRUCTIONS,
                    on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (local_read, local_write):
        await server.run(local_read, local_write, server.create_initialization_options())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Forward Codex stdio MCP to the persistent Collab MCP service")
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--execution-workspace", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.state_root.is_absolute():
        parser.error("--state-root must be absolute")
    workspace = None
    if args.execution_workspace is not None:
        workspace = args.execution_workspace.resolve()
        if not workspace.is_dir():
            parser.error("--execution-workspace must be an existing directory")
    asyncio.run(serve_bridge(args.state_root.resolve(), workspace))


if __name__ == "__main__":
    main()
