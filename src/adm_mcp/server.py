"""MCP server (streamable HTTP) with bearer token authentication."""

from __future__ import annotations

import hmac
import logging
import os

import uvicorn
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse, PlainTextResponse

from . import core

log = logging.getLogger("asustor-adm-mcp")

# All four MCP hints, explicit booleans (required by e.g. the OpenAI directory).
# Every tool only reads from the local NAS (/proc, /sys, allowed roots, and GET
# requests to the docker-socket-proxy), so one shared constant fits all of them.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

TOOLS = [
    core.get_system_info,
    core.list_disks,
    core.get_storage_usage,
    core.get_raid_status,
    core.list_directory,
    core.search_files,
    core.list_containers,
    core.list_compose_projects,
    core.inspect_container,
    core.get_container_logs,
    core.list_images,
    core.list_networks,
]


class BearerAuth:
    """ASGI middleware: requires `Authorization: Bearer <token>` (except /health)."""

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/health":
            given = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(given, self._expected):
                resp = JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


BASE_INSTRUCTIONS = (
    "Read-only queries for an Asustor NAS: system, disks, RAID, "
    "space, files (names only) and Docker state. Modifies nothing."
)


def build_instructions(notes: str = "") -> str:
    """Server instructions + owner notes (NAS_NOTES) so the client knows the
    real architecture and does not have to guess it."""
    notes = notes.replace("\\n", "\n").strip()
    if not notes:
        return BASE_INSTRUCTIONS
    return f"{BASE_INSTRUCTIONS}\n\nOwner notes about this NAS:\n{notes}"


def build_mcp(notes: str = "") -> FastMCP:
    mcp = FastMCP("asustor-adm-mcp", instructions=build_instructions(notes))
    for fn in TOOLS:
        mcp.tool(annotations=READ_ONLY)(fn)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request):
        return PlainTextResponse("ok")

    return mcp


def build_app(token: str, notes: str = ""):
    if len(token) < 24:
        raise SystemExit("MCP_TOKEN must be at least 24 characters long (openssl rand -hex 32)")
    mcp = build_mcp(notes)
    return BearerAuth(mcp.http_app(transport="streamable-http"), token)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = build_app(os.getenv("MCP_TOKEN", ""), os.getenv("NAS_NOTES", ""))
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8088"))
    log.info("Listening on http://%s:%s/mcp", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
