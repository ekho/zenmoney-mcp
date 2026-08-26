import inspect
import json

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
async def test_call_tool_requires_and_accepts_hardened_database():
    assert inspect.signature(server.call_tool).parameters["db"].annotation == (
        HardenedDatabase | None
    )

    db = HardenedDatabase(":memory:")
    db.init_schema()
    conn = db.connect()
    conn.execute(
        "INSERT INTO instruments(id,title,short_title,symbol,rate,changed) "
        "VALUES (1,'Ruble','RUB','₽',1,1)"
    )
    conn.execute(
        "INSERT INTO users(id,login,currency,parent,month_start_day,changed) "
        "VALUES (1,'u',1,NULL,1,1)"
    )
    conn.execute(
        "INSERT INTO accounts(id,title,type,instrument,balance,in_balance,savings,archive,user,changed) "
        "VALUES ('cash','Cash','checking',1,1000,1,0,0,1,1)"
    )
    conn.commit()
    try:
        content = await server.call_tool("get_net_worth", {}, db=db)
        assert json.loads(content[0].text)["net_worth"] == 1000
    finally:
        db.close()


@pytest.mark.asyncio
async def test_tool_discovery_applies_hardening_without_registration_patch():
    descriptors = {tool.name: tool for tool in await server.list_tools()}
    tools = {name: tool.input_schema for name, tool in descriptors.items()}

    spending = tools["analyze_spending"]["properties"]
    assert "include_transfers" not in spending
    assert spending["top_n"]["minimum"] == 1
    assert spending["top_n"]["maximum"] == 100
    assert spending["start_date"]["pattern"] == r"^\d{4}-\d{2}-\d{2}$"
    assert not ({"force_sync", "get_sync_status"} & set(tools))
    assert {
        *server.PREPARE_TOOL_ENTITIES,
        "prepare_mixed_changes",
        "get_change_proposal",
        "apply_changes",
    } <= set(tools)
    assert descriptors["prepare_transaction_changes"].annotations.read_only_hint is False
    assert descriptors["prepare_transaction_changes"].annotations.destructive_hint is False
    assert descriptors["get_change_proposal"].annotations.read_only_hint is True
    assert descriptors["apply_changes"].annotations.destructive_hint is True
    assert descriptors["apply_changes"].annotations.open_world_hint is True


@pytest.mark.asyncio
async def test_entity_read_tools_are_discoverable_locally_and_remotely():
    expected = {
        "list_accounts", "get_account",
        "list_tags", "get_tag",
        "list_merchants", "get_merchant",
        "list_reminders", "get_reminder",
        "list_reminder_markers", "get_reminder_marker",
        "list_transactions", "get_transaction",
        "list_budgets", "get_budget",
    }

    for remote in (False, True):
        descriptors = {
            tool.name: tool for tool in await server.list_tools(remote=remote)
        }
        assert expected <= descriptors.keys()
        assert all(
            descriptors[name].annotations.read_only_hint is True
            and descriptors[name].annotations.destructive_hint is False
            and descriptors[name].annotations.open_world_hint is False
            for name in expected
        )

    list_schema = descriptors["list_tags"].input_schema
    assert set(list_schema["properties"]) == {
        "limit", "cursor", "include_inactive"
    }
    assert list_schema["additionalProperties"] is False
    assert list_schema["properties"]["limit"]["maximum"] == 200
    assert descriptors["get_tag"].input_schema["required"] == ["id"]
    assert descriptors["get_budget"].input_schema["required"] == ["key"]


@pytest.mark.asyncio
async def test_change_tool_schemas_are_bounded_strict_and_entity_specific():
    tools = {tool.name: tool for tool in await server.list_tools()}

    for name in server.PREPARE_TOOL_ENTITIES:
        schema = tools[name].input_schema
        operations = schema["properties"]["operations"]
        assert schema["additionalProperties"] is False
        assert (operations["minItems"], operations["maxItems"]) == (1, 100)
        assert all(
            "entity" not in branch["properties"]
            and branch["additionalProperties"] is False
            for branch in operations["items"]["oneOf"]
        )

    mixed = tools["prepare_mixed_changes"].input_schema[
        "properties"
    ]["operations"]["items"]["oneOf"]
    assert {branch["properties"]["entity"]["const"] for branch in mixed} == {
        *server.PREPARE_TOOL_ENTITIES.values()
    }
    assert tools["apply_changes"].input_schema["required"] == ["proposal_id"]


@pytest.mark.asyncio
async def test_local_transaction_change_tools_prepare_and_apply_exact_proposal(
    tmp_path, monkeypatch
):
    db = HardenedDatabase(":memory:")
    db.init_schema()
    db.upsert_instruments([{"id": 1, "rate": 1, "changed": 1}])
    db.upsert_users([{"id": 1, "currency": 1, "changed": 1}])
    db.upsert_accounts(
        [{"id": "cash", "type": "checking", "instrument": 1, "changed": 1}]
    )
    db.upsert_transactions(
        [{
            "id": "tx", "user": 1, "changed": 10, "created": 1,
            "date": "2026-08-25", "income": 0, "outcome": 10,
            "incomeAccount": "cash", "outcomeAccount": "cash",
            "incomeInstrument": 1, "outcomeInstrument": 1,
            "tag": [], "deleted": False,
        }]
    )
    db.set_meta("user_entity_raw_complete", "1")
    db.set_server_timestamp(10)

    class Engine:
        async def sync(self, force_full=False):
            return {"status": "synced"}

        async def push_changes(self, changes):
            db.upsert_transactions(changes["transaction"])
            return {"status": "synced"}

    monkeypatch.setattr(server, "get_sync_engine", Engine)
    mutation_path = tmp_path / "proposals.db"

    prepared = await server.call_tool(
        "prepare_transaction_changes",
        {"operations": [{"operation": "update", "id": "tx",
                          "set": {"comment": "fixed"}}]},
        db=db,
        mutation_path=mutation_path,
    )
    proposal_id = json.loads(prepared[0].text)["proposal_id"]
    applied = await server.call_tool(
        "apply_changes",
        {"proposal_id": proposal_id},
        db=db,
        mutation_path=mutation_path,
    )

    assert json.loads(applied[0].text)["status"] == "applied"
    assert db.get_transaction_raw("tx")["comment"] == "fixed"


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
