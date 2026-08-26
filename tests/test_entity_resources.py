from __future__ import annotations

import json

import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

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
    database.upsert_tags(
        [
            {"id": "food", "user": 1, "title": "Food", "changed": 1},
            {"id": "travel", "user": 1, "title": "Travel", "changed": 2},
        ]
    )
    database.upsert_merchants(
        [{"id": "shop", "user": 1, "title": "Shop", "changed": 1}]
    )
    database.upsert_reminders(
        [{"id": "reminder", "user": 1, "changed": 1}]
    )
    database.upsert_reminder_markers(
        [
            {
                "id": "marker",
                "user": 1,
                "date": "2026-08-26",
                "state": "planned",
                "changed": 1,
            }
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


@pytest.mark.asyncio
async def test_entity_read_tools_list_and_get_through_existing_resource_layer(db):
    listed = json.loads(
        (await server.call_tool("list_tags", {"limit": 1}, db=db))[0].text
    )
    exact = json.loads(
        (await server.call_tool("get_tag", {"id": "food"}, db=db))[0].text
    )
    budget = json.loads(
        (
            await server.call_tool(
                "get_budget",
                {
                    "key": {
                        "owner_user_id": 1,
                        "tag": None,
                        "date": "2026-08-01",
                    }
                },
                db=db,
            )
        )[0].text
    )

    assert [item["id"] for item in listed["items"]] == ["food"]
    assert listed["next_cursor"] is not None
    assert exact == {
        "changed": 1,
        "id": "food",
        "owner_user_id": 1,
        "title": "Food",
    }
    assert budget["owner_user_id"] == 1
    assert budget["tag"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("list_name", "get_name", "get_arguments", "identity"),
    [
        ("list_accounts", "get_account", {"id": "a"}, {"id": "a"}),
        ("list_tags", "get_tag", {"id": "food"}, {"id": "food"}),
        (
            "list_merchants", "get_merchant", {"id": "shop"},
            {"id": "shop"},
        ),
        (
            "list_reminders", "get_reminder", {"id": "reminder"},
            {"id": "reminder"},
        ),
        (
            "list_reminder_markers", "get_reminder_marker",
            {"id": "marker"}, {"id": "marker"},
        ),
        (
            "list_transactions", "get_transaction", {"id": "active"},
            {"id": "active"},
        ),
        (
            "list_budgets",
            "get_budget",
            {
                "key": {
                    "owner_user_id": 1,
                    "tag": None,
                    "date": "2026-08-01",
                }
            },
            {"owner_user_id": 1, "tag": None, "date": "2026-08-01"},
        ),
    ],
)
async def test_every_entity_read_tool_maps_to_its_resource(
    db, list_name, get_name, get_arguments, identity
):
    listed = json.loads((await server.call_tool(list_name, {}, db=db))[0].text)
    exact = json.loads(
        (await server.call_tool(get_name, get_arguments, db=db))[0].text
    )

    assert any(
        all(item.get(key) == value for key, value in identity.items())
        for item in listed["items"]
    )
    assert all(exact[key] == value for key, value in identity.items())


@pytest.mark.asyncio
async def test_list_entity_tool_forwards_cursor_and_include_inactive(db):
    first = json.loads(
        (await server.call_tool("list_accounts", {"limit": 1}, db=db))[0].text
    )
    second = json.loads(
        (
            await server.call_tool(
                "list_accounts",
                {"limit": 1, "cursor": first["next_cursor"]},
                db=db,
            )
        )[0].text
    )
    including_inactive = json.loads(
        (
            await server.call_tool(
                "list_accounts", {"include_inactive": True}, db=db
            )
        )[0].text
    )

    assert [item["id"] for item in first["items"]] == ["a"]
    assert [item["id"] for item in second["items"]] == ["b"]
    assert [item["id"] for item in including_inactive["items"]] == [
        "a", "b", "c"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("list_tags", {"limit": 0}),
        ("list_tags", {"extra": True}),
        ("get_tag", {}),
        ("get_budget", {"key": {"owner_user_id": 1}}),
    ],
)
async def test_entity_read_tools_reject_invalid_arguments(db, name, arguments):
    with pytest.raises(MCPError) as error:
        await server.call_tool(name, arguments, db=db)

    assert error.value.code == INVALID_PARAMS
    assert error.value.message == "Invalid tool arguments"
