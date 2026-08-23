from __future__ import annotations

from types import SimpleNamespace

import pytest

from zenmoney_mcp.entrypoint import install_hardening
from zenmoney_mcp.financial_correctness import get_net_worth
from zenmoney_mcp.hardened_database import HardenedDatabase
from zenmoney_mcp.hardened_sync import HardenedSyncEngine


def test_entrypoint_installs_hardened_globals_before_server_start():
    fake_legacy = SimpleNamespace(
        analyze_income=lambda *args, **kwargs: None,
        analyze_merchants=lambda *args, **kwargs: None,
        analyze_transfers=lambda *args, **kwargs: None,
        analyze_trends=lambda *args, **kwargs: None,
        detect_anomalies=lambda *args, **kwargs: None,
        detect_recurring=lambda *args, **kwargs: None,
        get_upcoming_payments=lambda *args, **kwargs: None,
    )
    fake_server = SimpleNamespace(_db="stale", _sync_engine="stale")

    install_hardening(fake_server, fake_legacy)

    assert fake_server.Database is HardenedDatabase
    assert fake_server.SyncEngine is HardenedSyncEngine
    assert fake_server.get_net_worth is get_net_worth
    assert fake_server._db is None
    assert fake_server._sync_engine is None


def test_tool_discovery_advertises_runtime_bounds_and_removes_dead_flag():
    from zenmoney_mcp.entrypoint import harden_tool_schemas

    tools = [
        SimpleNamespace(
            name="analyze_spending",
            inputSchema={
                "type": "object",
                "properties": {
                    "top_n": {"type": "integer"},
                    "include_transfers": {"type": "boolean"},
                    "start_date": {"type": "string"},
                },
            },
        ),
        SimpleNamespace(
            name="search_transactions",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "min_amount": {"type": "number"},
                    "period": {"type": "string"},
                },
            },
        ),
        SimpleNamespace(
            name="get_exchange_rates",
            inputSchema={
                "type": "object",
                "properties": {
                    "currencies": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        SimpleNamespace(
            name="convert_currency",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_currency": {"type": "string"},
                    "to_currency": {"type": "string"},
                },
            },
        ),
    ]

    result = harden_tool_schemas(tools)
    spending = result[0].inputSchema["properties"]
    search = result[1].inputSchema["properties"]
    rates = result[2].inputSchema["properties"]["currencies"]
    conversion = result[3].inputSchema["properties"]

    assert "include_transfers" not in spending
    assert spending["top_n"]["minimum"] == 1
    assert spending["top_n"]["maximum"] == 100
    assert spending["start_date"]["pattern"] == r"^\d{4}-\d{2}-\d{2}$"
    assert search["limit"]["minimum"] == 1
    assert search["limit"]["maximum"] == 200
    assert search["min_amount"]["minimum"] == 0
    assert "this_month" in search["period"]["pattern"]
    assert rates["minItems"] == 1
    assert rates["maxItems"] == 20
    assert rates["uniqueItems"] is True
    assert conversion["from_currency"]["pattern"] == r"^[A-Za-z][A-Za-z0-9_-]{1,11}$"
    assert conversion["to_currency"]["maxLength"] == 12


@pytest.mark.asyncio
async def test_install_hardening_reregisters_list_tools_handler():
    class McpServer:
        def __init__(self):
            self.handler = None

        def list_tools(self):
            def register(handler):
                self.handler = handler
                return handler

            return register

    async def legacy_list_tools():
        return [
            SimpleNamespace(
                name="search_transactions",
                inputSchema={
                    "type": "object",
                    "properties": {"limit": {"type": "integer"}},
                },
            )
        ]

    fake_legacy = SimpleNamespace(
        analyze_income=lambda *args, **kwargs: None,
        analyze_merchants=lambda *args, **kwargs: None,
        analyze_transfers=lambda *args, **kwargs: None,
        analyze_trends=lambda *args, **kwargs: None,
        detect_anomalies=lambda *args, **kwargs: None,
        detect_recurring=lambda *args, **kwargs: None,
        get_upcoming_payments=lambda *args, **kwargs: None,
    )
    mcp_server = McpServer()
    fake_server = SimpleNamespace(
        _db=None,
        _sync_engine=None,
        server=mcp_server,
        list_tools=legacy_list_tools,
    )

    install_hardening(fake_server, fake_legacy)
    tools = await mcp_server.handler()

    assert tools[0].inputSchema["properties"]["limit"]["maximum"] == 200


def test_install_hardening_closes_preexisting_database_instance():
    class ExistingDatabase:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    existing = ExistingDatabase()
    fake_legacy = SimpleNamespace(
        analyze_income=lambda *args, **kwargs: None,
        analyze_merchants=lambda *args, **kwargs: None,
        analyze_transfers=lambda *args, **kwargs: None,
        analyze_trends=lambda *args, **kwargs: None,
        detect_anomalies=lambda *args, **kwargs: None,
        detect_recurring=lambda *args, **kwargs: None,
        get_upcoming_payments=lambda *args, **kwargs: None,
    )
    fake_server = SimpleNamespace(_db=existing, _sync_engine="stale")

    install_hardening(fake_server, fake_legacy)

    assert existing.closed is True
    assert fake_server._db is None


def test_server_cache_directory_uses_owner_only_permissions(tmp_path, monkeypatch):
    from zenmoney_mcp import server

    previous_db = server._db
    previous_database_class = server.Database
    monkeypatch.setattr(server.Path, "home", lambda: tmp_path)
    server.Database = HardenedDatabase
    server._db = None
    try:
        db = server.get_db()
        cache_dir = tmp_path / ".cache" / "zenmoney-mcp"
        assert cache_dir.stat().st_mode & 0o777 == 0o700
        assert (cache_dir / "zenmoney.db").stat().st_mode & 0o777 == 0o600
        db.close()
    finally:
        server._db = previous_db
        server.Database = previous_database_class
