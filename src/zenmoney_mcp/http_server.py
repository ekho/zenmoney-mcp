"""Remote read-only Streamable HTTP runtime."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .hardened_database import HardenedDatabase
from .server import create_server, get_database_path


def create_app(db_path: str | Path | None = None) -> Starlette:
    """Create the remote MCP ASGI application."""
    snapshot_path = Path(db_path) if db_path is not None else get_database_path()
    mcp_app = create_server(remote=True, db_path=snapshot_path).streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=os.environ.get("ZENMONEY_HTTP_HOST", "0.0.0.0"),
    )

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def readyz(request: Request) -> JSONResponse:
        database = HardenedDatabase(snapshot_path, read_only=True)
        try:
            ready = snapshot_path.is_file() and database.check_ready()
        finally:
            database.close()
        return JSONResponse(
            {"status": "ready" if ready else "not_ready"},
            status_code=200 if ready else 503,
        )

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/readyz", readyz),
            Mount("/", app=mcp_app),
        ],
        lifespan=mcp_app.router.lifespan_context,
    )


def main() -> None:
    """Run the remote MCP server."""
    uvicorn.run(
        create_app(),
        host=os.environ.get("ZENMONEY_HTTP_HOST", "0.0.0.0"),
        port=int(os.environ.get("ZENMONEY_HTTP_PORT", "8000")),
        access_log=False,
    )
