import pytest

from zenmoney_mcp import server
from zenmoney_mcp import financial_correctness as corrected
from zenmoney_mcp.hardened_database import HardenedDatabase
from zenmoney_mcp.hardened_sync import HardenedSyncEngine


def test_server_factory_constructs_local_and_remote_servers():
    assert server.create_server(remote=False).name == "zenmoney-mcp"
    assert server.create_server(remote=True).name == "zenmoney-mcp"


def test_server_uses_hardened_runtime_dependencies():
    assert server.HardenedDatabase is HardenedDatabase
    assert server.HardenedSyncEngine is HardenedSyncEngine
    assert server.get_net_worth is corrected.get_net_worth


@pytest.mark.asyncio
async def test_tool_discovery_applies_hardening_without_registration_patch():
    tools = {tool.name: tool.input_schema for tool in await server.list_tools()}

    spending = tools["analyze_spending"]["properties"]
    assert "include_transfers" not in spending
    assert spending["top_n"]["minimum"] == 1
    assert spending["top_n"]["maximum"] == 100
    assert spending["start_date"]["pattern"] == r"^\d{4}-\d{2}-\d{2}$"


def test_server_cache_directory_uses_owner_only_permissions(tmp_path, monkeypatch):
    previous_db = server._db
    monkeypatch.setattr(server.Path, "home", lambda: tmp_path)
    server._db = None
    try:
        db = server.get_db()
        cache_dir = tmp_path / ".cache" / "zenmoney-mcp"
        assert cache_dir.stat().st_mode & 0o777 == 0o700
        assert (cache_dir / "zenmoney.db").stat().st_mode & 0o777 == 0o600
        db.close()
    finally:
        server._db = previous_db
