from __future__ import annotations

import json

import pytest

from zenmoney_mcp import server
from zenmoney_mcp.entity_resources import (
    EntityResourceError,
    get_entity_resource,
    list_entity_resource,
)
from zenmoney_mcp.hardened_database import HardenedDatabase


@pytest.fixture
def db() -> HardenedDatabase:
    database = HardenedDatabase(":memory:")
    database.init_schema()
    database.upsert_accounts(
        [
            {"id": "a", "user": 1, "title": "A", "type": "cash",
             "instrument": 1, "balance": 1, "archive": False, "changed": 1,
             "future": "private"},
            {"id": "b", "user": 1, "title": "B", "type": "cash",
             "instrument": 1, "balance": 2, "archive": False, "changed": 2},
            {"id": "c", "user": 1, "title": "C", "type": "cash",
             "instrument": 1, "balance": 3, "archive": True, "changed": 3},
        ]
    )
    database.upsert_transactions(
        [
            {"id": "active", "user": 1, "date": "2026-08-25", "created": 1,
             "income": 0, "outcome": 1, "incomeAccount": "a",
             "outcomeAccount": "a", "incomeInstrument": 1,
             "outcomeInstrument": 1, "deleted": False, "changed": 1},
            {"id": "deleted", "user": 1, "date": "2026-08-24", "created": 1,
             "income": 0, "outcome": 1, "incomeAccount": "a",
             "outcomeAccount": "a", "incomeInstrument": 1,
             "outcomeInstrument": 1, "deleted": True, "changed": 2},
        ]
    )
    database.upsert_budgets(
        [
            {"user": 1, "tag": None, "date": "2026-08-01", "income": 0,
             "incomeLock": False, "outcome": 100, "outcomeLock": True,
             "changed": 1},
            {"user": 1, "tag": "food", "date": "2026-08-01", "income": 0,
             "incomeLock": False, "outcome": 0, "outcomeLock": False,
             "changed": 2},
        ]
    )
    return database


def test_collection_cursor_is_stable_opaque_and_entity_bound(db):
    first = list_entity_resource(db, "account", limit=1)
    second = list_entity_resource(
        db, "account", limit=1, cursor=first["next_cursor"]
    )

    assert first["items"][0]["id"] == "a"
    assert second["items"][0]["id"] == "b"
    assert first["next_cursor"] != '"a"'
    assert second["next_cursor"] is None
    with pytest.raises(EntityResourceError, match="cursor"):
        list_entity_resource(db, "transaction", cursor=first["next_cursor"])


def test_collection_filters_inactive_by_default_and_can_include_it(db):
    assert [item["id"] for item in list_entity_resource(db, "account")["items"]] == [
        "a", "b"
    ]
    assert [
        item["id"]
        for item in list_entity_resource(db, "account", include_inactive=True)["items"]
    ] == ["a", "b", "c"]
    assert [
        item["id"] for item in list_entity_resource(db, "transaction")["items"]
    ] == ["active"]
    assert len(list_entity_resource(db, "budget")["items"]) == 1
    assert len(
        list_entity_resource(db, "budget", include_inactive=True)["items"]
    ) == 2


@pytest.mark.parametrize("limit", [0, 201, True, "50"])
def test_collection_rejects_invalid_limits(db, limit):
    with pytest.raises(EntityResourceError, match="limit"):
        list_entity_resource(db, "account", limit=limit)


@pytest.mark.parametrize("cursor", ["", "***", "e30", "WzEsMl0"])
def test_collection_rejects_invalid_cursors(db, cursor):
    with pytest.raises(EntityResourceError, match="cursor"):
        list_entity_resource(db, "account", cursor=cursor)


def test_exact_resource_returns_inactive_but_never_unknown_raw_fields(db):
    result = get_entity_resource(db, "account", "c")

    assert result["id"] == "c"
    assert result["archive"] is True
    assert result["owner_user_id"] == 1
    assert "future" not in get_entity_resource(db, "account", "a")
    assert "raw_json" not in str(result)


def test_budget_exact_resource_uses_typed_composite_key(db):
    result = get_entity_resource(
        db,
        "budget",
        {"owner_user_id": 1, "date": "2026-08-01", "tag": None},
    )

    assert result["owner_user_id"] == 1
    assert result["tag"] is None
    assert result["outcome"] == 100


def test_unknown_entity_and_key_fail_with_fixed_errors(db):
    with pytest.raises(EntityResourceError, match="entity_type_invalid"):
        list_entity_resource(db, "user")
    with pytest.raises(EntityResourceError, match="entity_not_found"):
        get_entity_resource(db, "account", "missing")


@pytest.mark.asyncio
async def test_server_dispatches_collection_and_exact_entity_uris(db):
    collection = json.loads(
        await server.read_resource("zenmoney://accounts?limit=1", db=db)
    )
    exact = json.loads(await server.read_resource("zenmoney://accounts/c", db=db))
    budget = json.loads(
        await server.read_resource(
            "zenmoney://budgets/1/2026-08-01/null", db=db
        )
    )

    assert len(collection["items"]) == 1
    assert collection["next_cursor"] is not None
    assert exact["archive"] is True
    assert budget["tag"] is None
