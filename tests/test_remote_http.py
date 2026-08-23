"""Streamable HTTP tests for the remote read-only MCP surface."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from zenmoney_mcp import server
from zenmoney_mcp.hardened_database import HardenedDatabase
from zenmoney_mcp.http_server import create_app


def _write_snapshot(path, timestamp: int, balance: float = 1000) -> None:
    database = HardenedDatabase(path)
    database.init_schema()
    conn = database.connect()
    conn.execute(
        "INSERT INTO instruments(id,title,short_title,symbol,rate,changed) "
        "VALUES (1,'Ruble','RUB','RUB',1,1)"
    )
    conn.execute(
        "INSERT INTO users(id,login,currency,parent,month_start_day,changed) "
        "VALUES (1,'user',1,NULL,1,1)"
    )
    conn.execute(
        "INSERT INTO accounts(id,title,type,instrument,balance,in_balance,savings,archive,user,changed) "
        "VALUES ('cash','Cash','checking',1,?,1,0,0,1,1)",
        (balance,),
    )
    database.set_meta("server_timestamp", str(timestamp))
    database.close()


@asynccontextmanager
async def _mcp_client(app):
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as http_client:
            async with streamable_http_client(
                "http://127.0.0.1/mcp",
                http_client=http_client,
                terminate_on_close=False,
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as client:
                    yield client


@pytest.mark.asyncio
async def test_remote_mcp_exposes_only_annotated_read_only_surface(tmp_path):
    path = tmp_path / "snapshot.db"
    _write_snapshot(path, 100)
    app = create_app(path)

    async with _mcp_client(app) as client:
        initialized = await client.initialize()
        tools = (await client.list_tools()).tools
        resources = (await client.list_resources()).resources
        result = await client.call_tool("get_net_worth", {})

    names = {tool.name for tool in tools}
    assert initialized.server_info.name == "zenmoney-mcp"
    assert "get_net_worth" in names
    assert not ({"sync_data", "suggest_category"} & names)
    assert {str(resource.uri) for resource in resources} >= {
        "zenmoney://accounts",
        "zenmoney://sync-status",
    }
    assert all(tool.annotations.read_only_hint is True for tool in tools)
    assert all(tool.annotations.destructive_hint is False for tool in tools)
    assert all(tool.annotations.open_world_hint is False for tool in tools)
    assert json.loads(result.content[0].text)["net_worth"] == 1000


@pytest.mark.asyncio
async def test_remote_mcp_rejects_excluded_and_unknown_tools_before_db_open(tmp_path):
    app = create_app(tmp_path / "missing.db")

    async with _mcp_client(app) as client:
        await client.initialize()
        with pytest.raises(MCPError):
            await client.call_tool("sync_data", {})
        with pytest.raises(MCPError):
            await client.call_tool("suggest_category", {"payee": "shop"})
        with pytest.raises(MCPError):
            await client.call_tool("not_registered", {})


@pytest.mark.asyncio
async def test_remote_dispatch_rejection_is_an_expected_fixed_mcp_error(tmp_path):
    with pytest.raises(MCPError, match="Remote tool is unavailable"):
        await server.call_tool(
            "sync_data",
            {},
            remote=True,
            db_path=tmp_path / "missing.db",
        )


@pytest.mark.asyncio
async def test_health_and_readiness_are_fixed_and_non_sensitive(tmp_path):
    ready_path = tmp_path / "ready.db"
    malformed_path = tmp_path / "malformed.db"
    _write_snapshot(ready_path, 100)
    malformed_path.write_bytes(b"private malformed database contents")

    cases = [
        (ready_path, 200, {"status": "ready"}),
        (tmp_path / "missing.db", 503, {"status": "not_ready"}),
        (malformed_path, 503, {"status": "not_ready"}),
    ]
    for path, expected_status, expected_json in cases:
        app = create_app(path)
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            health = await client.get("/healthz")
            readiness = await client.get("/readyz")

        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert readiness.status_code == expected_status
        assert readiness.json() == expected_json


@pytest.mark.asyncio
async def test_running_app_reads_atomically_replaced_snapshot(tmp_path):
    path = tmp_path / "snapshot.db"
    replacement = tmp_path / "replacement.db"
    _write_snapshot(path, 100)
    _write_snapshot(replacement, 200)
    app = create_app(path)

    async with _mcp_client(app) as client:
        await client.initialize()
        first = await client.read_resource("zenmoney://sync-status")
        os.replace(replacement, path)
        second = await client.read_resource("zenmoney://sync-status")

    assert json.loads(first.contents[0].text)["last_server_timestamp"] == 100
    assert json.loads(second.contents[0].text)["last_server_timestamp"] == 200


@pytest.mark.asyncio
async def test_explicit_remote_database_remains_caller_owned():
    database = HardenedDatabase(":memory:")
    database.init_schema()
    try:
        await server.read_resource(
            "zenmoney://sync-status", db=database, remote=True
        )
        assert database.connect().execute("SELECT 1").fetchone()[0] == 1
    finally:
        database.close()


@pytest.mark.asyncio
async def test_local_dispatch_keeps_shared_database_open(monkeypatch):
    database = HardenedDatabase(":memory:")
    database.init_schema()
    monkeypatch.setattr(server, "get_db", lambda: database)

    await server.read_resource("zenmoney://sync-status")

    assert database.connect().execute(
        "SELECT name FROM sqlite_master WHERE name = 'sync_meta'"
    ).fetchone() is not None
    database.close()


def test_http_cli_uses_configured_bind_and_disables_access_log(monkeypatch):
    from zenmoney_mcp import http_server

    calls = []
    monkeypatch.setenv("ZENMONEY_HTTP_HOST", "127.0.0.2")
    monkeypatch.setenv("ZENMONEY_HTTP_PORT", "8123")
    monkeypatch.setattr(http_server.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    http_server.main()

    assert len(calls) == 1
    assert calls[0][0][0].routes
    assert calls[0][1] == {
        "host": "127.0.0.2",
        "port": 8123,
        "access_log": False,
    }
