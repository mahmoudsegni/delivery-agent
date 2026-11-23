"""MCP endpoint contract (#16): the mounted Streamable HTTP app answers a
real JSON-RPC handshake and exposes the four read-only tools.

No DB, no network (test_forecast philosophy): we drive the full FastAPI app
in-process — through the /mcp mount and its lifespan — and only exercise
initialize/tools/list. The tools themselves are thin glue over services
tested elsewhere.
"""

import asyncio
import json

import httpx

from app.main import app

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def parse_sse(text: str) -> dict:
    """The server may answer application/json or text/event-stream."""
    if text.lstrip().startswith("{"):
        return json.loads(text)
    return json.loads(next(ln.removeprefix("data: ") for ln in text.splitlines() if ln.startswith("data: ")))


async def _drive() -> list[str]:
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            headers = {"Accept": "application/json, text/event-stream"}
            r = await c.post("/mcp", json=INIT, headers=headers)
            assert r.status_code == 200, r.text
            server_info = parse_sse(r.text)["result"]["serverInfo"]
            assert server_info["name"] == "delivery-agent"
            sid = r.headers.get("mcp-session-id")
            if sid:
                headers["mcp-session-id"] = sid
            await c.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers,
            )
            r = await c.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers
            )
            assert r.status_code == 200, r.text
            return [t["name"] for t in parse_sse(r.text)["result"]["tools"]]


def test_mcp_mount_handshake_and_tool_registry():
    names = asyncio.run(_drive())
    assert set(names) == {"delivery_snapshot", "backlog", "forecast", "sprints"}
