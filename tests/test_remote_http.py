"""Streamable HTTP tests for the private remote MCP surface."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import INTERNAL_ERROR, INVALID_PARAMS

from zenmoney_mcp import server
from zenmoney_mcp.hardened_database import HardenedDatabase
from zenmoney_mcp.http_server import create_app


def _write_snapshot(
    path, timestamp: int, balance: float = 1000, *, journal_mode: str | None = None
) -> None:
    kwargs = {"journal_mode": journal_mode} if journal_mode is not None else {}
    database = HardenedDatabase(path, **kwargs)
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
async def test_remote_mcp_exposes_truthfully_annotated_surface(tmp_path):
    path = tmp_path / "snapshot.db"
    control_path = tmp_path / "sync-state.json"
    _write_snapshot(path, 100)
    app = create_app(path, control_path)

    async with _mcp_client(app) as client:
        initialized = await client.initialize()
        tools = (await client.list_tools()).tools
        resources = (await client.list_resources()).resources
        result = await client.call_tool("get_net_worth", {})

    names = {tool.name for tool in tools}
    tools_by_name = {tool.name: tool for tool in tools}
    assert initialized.server_info.name == "zenmoney-mcp"
    assert "get_net_worth" in names
    assert {"force_sync", "get_sync_status"} <= names
    assert not ({"sync_data", "suggest_category"} & names)
    assert {str(resource.uri) for resource in resources} >= {
        "zenmoney://accounts",
        "zenmoney://sync-status",
    }
    assert tools_by_name["force_sync"].annotations.read_only_hint is False
    assert tools_by_name["force_sync"].annotations.open_world_hint is True
    assert tools_by_name["get_sync_status"].annotations.read_only_hint is True
    assert all(
        tool.annotations.read_only_hint is True
        for tool in tools
        if tool.name != "force_sync"
    )
    assert all(tool.annotations.destructive_hint is False for tool in tools)
    assert all(
        tool.annotations.open_world_hint is False
        for tool in tools
        if tool.name != "force_sync"
    )
    assert json.loads(result.content[0].text)["net_worth"] == 1000


@pytest.mark.asyncio
async def test_remote_force_sync_and_status_work_before_first_snapshot(tmp_path):
    control_path = tmp_path / "sync-state.json"
    app = create_app(tmp_path / "missing.db", control_path)

    async with _mcp_client(app) as client:
        await client.initialize()
        requested = await client.call_tool("force_sync", {"force_full": True})
        status = await client.call_tool("get_sync_status", {})

    requested_payload = json.loads(requested.content[0].text)
    status_payload = json.loads(status.content[0].text)
    assert requested_payload["status"] == "accepted"
    assert requested_payload["mode"] == "full"
    assert status_payload["state"] == "pending"
    assert status_payload["request_id"] == requested_payload["request_id"]
    assert status_payload["last_sync_time"] is None
    assert status_payload["staleness"] == "never_synced"
    assert status_payload["cache_stats"] == {}


@pytest.mark.asyncio
async def test_remote_sync_status_combines_control_and_snapshot_metadata(tmp_path):
    path = tmp_path / "snapshot.db"
    control_path = tmp_path / "sync-state.json"
    _write_snapshot(path, 321)
    database = HardenedDatabase(path)
    database.set_meta("last_sync_time", "1")
    database.close()
    app = create_app(path, control_path)

    async with _mcp_client(app) as client:
        await client.initialize()
        status = await client.call_tool("get_sync_status", {})

    payload = json.loads(status.content[0].text)
    assert payload["state"] == "idle"
    assert payload["last_server_timestamp"] == 321
    assert payload["last_sync_time"] is not None
    assert payload["cache_stats"]["accounts"] == 1


@pytest.mark.asyncio
async def test_remote_sync_control_rejects_invalid_state_without_echo(tmp_path):
    control_path = tmp_path / "sync-state.json"
    sentinel = "sensitive-control-content"
    control_path.write_text(sentinel, encoding="utf-8")
    app = create_app(tmp_path / "missing.db", control_path)

    async with _mcp_client(app) as client:
        await client.initialize()
        status = await client.call_tool("get_sync_status", {})
        requested = await client.call_tool("force_sync", {})

    assert json.loads(status.content[0].text) == {
        "state": "failed",
        "request_id": None,
        "mode": None,
        "requested_at": None,
        "started_at": None,
        "finished_at": None,
        "failure_code": "invalid_sync_state",
        "last_server_timestamp": 0,
        "last_sync_time": None,
        "cache_stats": {},
        "staleness": "never_synced",
    }
    assert json.loads(requested.content[0].text) == {
        "status": "rejected",
        "failure_code": "invalid_sync_state",
    }
    assert sentinel not in status.content[0].text
    assert sentinel not in requested.content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("force_sync", {"extra": 1}),
        ("force_sync", {"force_full": "yes"}),
        ("get_sync_status", {"extra": 1}),
    ],
)
async def test_remote_sync_tools_reject_arguments_outside_their_schema(
    tmp_path, name, arguments
):
    control_path = tmp_path / "sync-state.json"
    app = create_app(tmp_path / "missing.db", control_path)

    async with _mcp_client(app) as client:
        await client.initialize()
        with pytest.raises(MCPError) as error:
            await client.call_tool(name, arguments)

    assert error.value.code == INVALID_PARAMS
    assert error.value.message == "Invalid tool arguments"
    assert not control_path.exists()


@pytest.mark.asyncio
async def test_delete_journal_snapshot_works_from_truly_read_only_directory(tmp_path):
    path = tmp_path / "snapshot.db"
    _write_snapshot(path, 100, journal_mode="DELETE")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        connection.close()
    path.chmod(0o444)
    tmp_path.chmod(0o555)

    try:
        app = create_app(path)
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as http_client:
            readiness = await http_client.get("/readyz")
        async with _mcp_client(app) as client:
            await client.initialize()
            result = await client.call_tool("get_net_worth", {})
    finally:
        tmp_path.chmod(0o755)
        path.chmod(0o600)

    assert readiness.status_code == 200
    assert readiness.json() == {"status": "ready"}
    assert json.loads(result.content[0].text)["net_worth"] == 1000


@pytest.mark.asyncio
async def test_remote_mcp_rejects_excluded_and_unknown_tools_before_db_open(
    tmp_path, caplog
):
    caplog.set_level(logging.WARNING)
    app = create_app(tmp_path / "missing.db")

    async with _mcp_client(app) as client:
        await client.initialize()
        for name, arguments in (
            ("sync_data", {}),
            ("suggest_category", {"payee": "shop"}),
            ("not_registered", {}),
        ):
            with pytest.raises(MCPError) as error:
                await client.call_tool(name, arguments)
            assert error.value.code == INVALID_PARAMS
            assert error.value.message == "Remote tool is unavailable"
    assert caplog.records == []


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
async def test_remote_application_error_redacts_arguments_response_and_logs(
    monkeypatch, tmp_path, caplog
):
    sentinel = "account-sentinel-9b4e"
    path = tmp_path / "snapshot.db"
    _write_snapshot(path, 100)

    def fail(*args, **kwargs):
        raise RuntimeError(f"failed account {sentinel}")

    monkeypatch.setattr(server, "get_account_flow", fail)
    caplog.set_level(logging.WARNING)
    app = create_app(path)

    async with _mcp_client(app) as client:
        await client.initialize()
        with pytest.raises(MCPError) as error:
            await client.call_tool(
                "get_account_flow",
                {"account_id": sentinel, "period": "this_month"},
            )

    rendered = f"{error.value}\n{caplog.text}"
    assert sentinel not in rendered
    assert "Traceback" not in rendered
    assert error.value.code == INTERNAL_ERROR
    assert str(error.value) == "Remote tool failed"
    structured = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.getMessage().startswith("{")
    ]
    assert structured == [
        {
            "event": "remote_tool_call",
            "tool": "get_account_flow",
            "status": "failed",
            "exception_class": "RuntimeError",
        }
    ]


@pytest.mark.asyncio
async def test_remote_resource_error_redacts_exception_response_and_logs(
    monkeypatch, tmp_path, caplog
):
    sentinel = "resource-sentinel-c742"
    path = tmp_path / "snapshot.db"
    _write_snapshot(path, 100)

    def fail(*args, **kwargs):
        raise RuntimeError(f"resource failure {sentinel}")

    monkeypatch.setattr(server, "get_accounts_resource", fail)
    caplog.set_level(logging.WARNING)
    app = create_app(path)

    async with _mcp_client(app) as client:
        await client.initialize()
        with pytest.raises(MCPError) as error:
            await client.read_resource("zenmoney://accounts")

    rendered = f"{error.value}\n{caplog.text}"
    assert sentinel not in rendered
    assert "Traceback" not in rendered
    assert error.value.code == INTERNAL_ERROR
    assert error.value.message == "Remote resource failed"
    structured = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.getMessage().startswith("{")
    ]
    assert structured == [
        {
            "event": "remote_resource_read",
            "status": "failed",
            "exception_class": "RuntimeError",
        }
    ]


@pytest.mark.asyncio
async def test_remote_unknown_resource_preserves_safe_invalid_params_error(
    tmp_path, caplog
):
    sentinel = "resource-sentinel-unknown-67d1"
    caplog.set_level(logging.WARNING)
    app = create_app(tmp_path / "missing.db")

    async with _mcp_client(app) as client:
        await client.initialize()
        with pytest.raises(MCPError) as error:
            await client.read_resource(f"zenmoney://{sentinel}")

    rendered = f"{error.value}\n{caplog.text}"
    assert sentinel not in rendered
    assert "Traceback" not in rendered
    assert error.value.code == INVALID_PARAMS
    assert error.value.message == "Remote resource is unavailable"
    assert caplog.records == []


@pytest.mark.asyncio
async def test_local_resource_keeps_application_exception(monkeypatch):
    sentinel = "local-resource-sentinel"
    database = HardenedDatabase(":memory:")
    database.init_schema()

    def fail(*args, **kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(server, "get_accounts_resource", fail)
    try:
        with pytest.raises(RuntimeError, match=sentinel):
            await server.read_resource("zenmoney://accounts", db=database)
    finally:
        database.close()


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
