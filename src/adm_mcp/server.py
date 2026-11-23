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

# The only write action. Disruptive but not destructive: the container comes
# back up. Same four explicit booleans as above.
WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
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

# Registered only when ALLOW_WRITE=1 (see build_mcp).
WRITE_TOOLS = [
    core.restart_container,
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

WRITE_INSTRUCTIONS = (
    "Write mode is ON: the only action available is restart_container, limited "
    "to the owner's allowlist. Everything else is read-only. Ask the person "
    "before restarting anything, and never retry a failed restart in a loop."
)


def build_instructions(notes: str = "", write: bool = False) -> str:
    """Server instructions + owner notes (NAS_NOTES) so the client knows the
    real architecture and does not have to guess it."""
    base = f"{BASE_INSTRUCTIONS}\n\n{WRITE_INSTRUCTIONS}" if write else BASE_INSTRUCTIONS
    notes = notes.replace("\\n", "\n").strip()
    if not notes:
        return base
    return f"{base}\n\nOwner notes about this NAS:\n{notes}"


def build_mcp(notes: str = "", write: bool | None = None) -> FastMCP:
    write = core.cfg.write_enabled if write is None else write
    mcp = FastMCP("asustor-adm-mcp", instructions=build_instructions(notes, write))
    for fn in TOOLS:
        mcp.tool(annotations=READ_ONLY)(fn)
    if write:
        for fn in WRITE_TOOLS:
            mcp.tool(annotations=WRITE)(fn)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request):
        return PlainTextResponse("ok")

    return mcp


def build_app(token: str, notes: str = ""):
    if len(token) < 24:
        raise SystemExit("MCP_TOKEN must be at least 24 characters long (openssl rand -hex 32)")
    core.validate_write_config()
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
