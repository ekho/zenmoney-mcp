from __future__ import annotations

import math
import uuid

import pytest

from zenmoney_mcp.entity_changes import (
    MutationValidationError,
    normalize_operations,
    rebuild_after,
    verify_after,
)
from zenmoney_mcp.hardened_database import HardenedDatabase, entity_key


@pytest.fixture
def financial_db() -> HardenedDatabase:
    db = HardenedDatabase(":memory:")
    db.init_schema()
    db.upsert_instruments(
        [
            {
                "id": 1,
                "title": "Ruble",
                "shortTitle": "RUB",
                "symbol": "RUB",
                "rate": 1,
                "changed": 1,
            }
        ]
    )
    db.upsert_users(
        [{"id": 1, "login": "user", "currency": 1, "parent": None, "changed": 1}]
    )
    db.upsert_accounts(
        [
            {
                "id": "cash",
                "title": "Cash",
                "type": "checking",
                "instrument": 1,
                "balance": 100,
                "startBalance": 100,
                "inBalance": True,
                "savings": False,
                "enableCorrection": False,
                "enableSMS": False,
                "archive": False,
                "user": 1,
                "changed": 1,
            }
        ]
    )
    db.upsert_tags(
        [
            {
                "id": "food",
                "title": "Food",
                "showIncome": False,
                "showOutcome": True,
                "budgetIncome": False,
                "budgetOutcome": True,
                "user": 1,
                "changed": 1,
            }
        ]
    )
    db.upsert_merchants(
        [{"id": "shop", "title": "Shop", "user": 1, "changed": 1}]
    )
    db.upsert_reminders(
        [
            {
                "id": "scheduled",
                "user": 1,
                "income": 0,
                "outcome": 1,
                "incomeAccount": "cash",
                "outcomeAccount": "cash",
                "incomeInstrument": 1,
                "outcomeInstrument": 1,
                "tag": None,
                "merchant": None,
                "payee": None,
                "comment": None,
                "interval": None,
                "step": None,
                "points": None,
                "startDate": "2026-08-25",
                "endDate": None,
                "notify": False,
                "changed": 1,
            }
        ]
    )
    db.upsert_reminder_markers(
        [
            {
                "id": "marker",
                "user": 1,
                "income": 0,
                "outcome": 1,
                "incomeAccount": "cash",
                "outcomeAccount": "cash",
                "incomeInstrument": 1,
                "outcomeInstrument": 1,
                "tag": None,
                "merchant": None,
                "payee": None,
                "comment": None,
                "date": "2026-08-25",
                "reminder": "scheduled",
                "state": "planned",
                "notify": False,
                "changed": 1,
            }
        ]
    )
    db.upsert_transactions(
        [
            {
                "id": "transaction",
                "user": 1,
                "created": 1,
                "date": "2026-08-25",
                "income": 0,
                "outcome": 1,
                "incomeAccount": "cash",
                "outcomeAccount": "cash",
                "incomeInstrument": 1,
                "outcomeInstrument": 1,
                "tag": None,
                "merchant": None,
                "payee": None,
                "comment": None,
                "deleted": False,
                "changed": 1,
            }
        ]
    )
    db.upsert_budgets(
        [
            {
                "user": 1,
                "tag": "food",
                "date": "2026-08-01",
                "income": 0,
                "incomeLock": False,
                "outcome": 100,
                "outcomeLock": True,
                "changed": 1,
            }
        ]
    )
    db.set_meta("user_entity_raw_complete", "1")
    return db


def test_mixed_create_resolves_typed_refs_and_owner(financial_db):
    items = normalize_operations(
        financial_db,
        [
            {
                "entity": "tag",
                "operation": "create",
                "ref": "new_food",
                "value": {
                    "title": "MCP TEST Food",
                    "showIncome": False,
                    "showOutcome": True,
                    "budgetIncome": False,
                    "budgetOutcome": True,
                },
            },
            {
                "entity": "transaction",
                "operation": "create",
                "ref": "lunch",
                "value": {
                    "date": "2026-08-25",
                    "income": 0,
                    "outcome": 10,
                    "incomeAccount": "cash",
                    "outcomeAccount": "cash",
                    "incomeInstrument": 1,
                    "outcomeInstrument": 1,
                    "tag": [{"ref": "new_food"}],
                },
            },
        ],
        now=100,
    )

    assert items[0]["entity_type"] == "tag"
    assert items[0]["resolved"]["user"] == 1
    assert items[0]["resolved"]["changed"] == 100
    assert items[1]["resolved"]["tag"] == [items[0]["entity_id"]]
    assert items[1]["resolved"]["user"] == 1
    assert items[1]["resolved"]["created"] == 100


def test_split_normalizes_exact_parts_and_preserves_bank_metadata(financial_db):
    financial_db.upsert_tags(
        [{"id": "other", "title": "Other", "showIncome": False,
          "showOutcome": True, "budgetIncome": False, "budgetOutcome": True,
          "user": 1, "changed": 1}]
    )
    raw = financial_db.get_transaction_raw("transaction")
    financial_db.upsert_transactions(
        [{
            **raw,
            "outcome": 100,
            "opOutcome": 50,
            "opOutcomeInstrument": 1,
            "outcomeBankID": "bank-operation",
            "originalPayee": "Original shop",
            "future": {"kept": True},
            "created": 2,
            "changed": 10,
        }]
    )

    items = normalize_operations(
        financial_db,
        [{
            "operation": "split",
            "transaction_id": "transaction",
            "parts": [
                {"amount": 30, "category_id": "food"},
                {"amount": "remainder", "category_id": "other"},
            ],
        }],
        entity_type="transaction",
        now=100,
    )

    assert [item["operation"] for item in items] == ["update", "create"]
    assert items[0]["entity_id"] == "transaction"
    assert items[0]["expected_changed"] == 10
    assert items[0]["after"] == {
        "outcome": 30.0,
        "opOutcome": 15.0,
        "tag": ["food"],
    }
    created = items[1]["after"]
    assert uuid.UUID(created["id"]).version == 4
    assert created["outcome"] == 70
    assert created["opOutcome"] == 35
    assert created["tag"] == ["other"]
    assert created["outcomeBankID"] == "bank-operation"
    assert created["originalPayee"] == "Original shop"
    assert created["future"] == {"kept": True}
    assert created["created"] == 2
    assert sum(item["after"]["outcome"] for item in items) == 100
    assert sum(item["after"]["opOutcome"] for item in items) == 50


def test_split_keeps_rounded_operation_amounts_non_negative(financial_db):
    raw = financial_db.get_transaction_raw("transaction")
    financial_db.upsert_transactions(
        [{**raw, "outcome": 1, "opOutcome": 0.5, "opOutcomeInstrument": 1}]
    )

    items = normalize_operations(
        financial_db,
        [{
            "operation": "split",
            "transaction_id": "transaction",
            "parts": [
                *[{"amount": 0.01, "category_id": "food"} for _ in range(99)],
                {"amount": "remainder", "category_id": "food"},
            ],
        }],
        entity_type="transaction",
    )

    operation_amounts = [item["after"]["opOutcome"] for item in items]
    assert min(operation_amounts) >= 0
    assert sum(operation_amounts) == 0.5


@pytest.mark.parametrize(
    "parts",
    [
        [
            {"amount": 30, "category_id": "food"},
            {"amount": 60, "category_id": "food"},
        ],
        [
            {"amount": "remainder", "category_id": "food"},
            {"amount": "remainder", "category_id": "food"},
        ],
        [
            {"amount": 100, "category_id": "food"},
            {"amount": "remainder", "category_id": "food"},
        ],
        [{"amount": 100, "category_id": "food"}],
    ],
)
def test_split_rejects_invalid_part_totals(financial_db, parts):
    raw = financial_db.get_transaction_raw("transaction")
    financial_db.upsert_transactions([{**raw, "outcome": 100, "changed": 10}])

    with pytest.raises(MutationValidationError):
        normalize_operations(
            financial_db,
            [{
                "operation": "split",
                "transaction_id": "transaction",
                "parts": parts,
            }],
            entity_type="transaction",
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"income": 100, "outcome": 100},
        {"hold": True, "outcome": 100},
        {"deleted": True, "outcome": 100},
    ],
)
def test_split_rejects_transfer_hold_and_deleted_source(financial_db, patch):
    raw = financial_db.get_transaction_raw("transaction")
    financial_db.upsert_transactions([{**raw, **patch, "changed": 10}])

    with pytest.raises(MutationValidationError):
        normalize_operations(
            financial_db,
            [{
                "entity": "transaction",
                "operation": "split",
                "transaction_id": "transaction",
                "parts": [
                    {"amount": 1, "category_id": "food"},
                    {"amount": "remainder", "category_id": "food"},
                ],
            }],
        )


def test_mixed_create_supports_all_seven_user_entity_types(financial_db):
    items = normalize_operations(
        financial_db,
        [
            {"entity": "tag", "operation": "create", "ref": "tag",
             "value": {"title": "MCP TEST Category"}},
            {"entity": "merchant", "operation": "create", "ref": "merchant",
             "value": {"title": "MCP TEST Merchant"}},
            {"entity": "account", "operation": "create", "ref": "account",
             "value": {"title": "MCP TEST Cash", "type": "cash", "instrument": 1,
                       "startBalance": 0}},
            {"entity": "reminder", "operation": "create", "ref": "reminder",
             "value": {"income": 0, "outcome": 1,
                       "incomeAccount": {"ref": "account"},
                       "outcomeAccount": {"ref": "account"},
                       "incomeInstrument": 1, "outcomeInstrument": 1,
                       "tag": [{"ref": "tag"}], "merchant": {"ref": "merchant"},
                       "startDate": "2026-08-25"}},
            {"entity": "reminderMarker", "operation": "create", "ref": "marker",
             "value": {"income": 0, "outcome": 1,
                       "incomeAccount": {"ref": "account"},
                       "outcomeAccount": {"ref": "account"},
                       "incomeInstrument": 1, "outcomeInstrument": 1,
                       "tag": [{"ref": "tag"}], "merchant": {"ref": "merchant"},
                       "date": "2026-08-25", "reminder": {"ref": "reminder"},
                       "state": "planned"}},
            {"entity": "transaction", "operation": "create", "ref": "transaction",
             "value": {"date": "2026-08-25", "income": 0, "outcome": 1,
                       "incomeAccount": {"ref": "account"},
                       "outcomeAccount": {"ref": "account"},
                       "incomeInstrument": 1, "outcomeInstrument": 1,
                       "tag": [{"ref": "tag"}], "merchant": {"ref": "merchant"}}},
            {"entity": "budget", "operation": "create",
             "value": {"date": "2026-08-01", "tag": {"ref": "tag"},
                       "income": 0, "incomeLock": False,
                       "outcome": 100, "outcomeLock": True}},
        ],
        now=100,
    )

    assert [item["entity_type"] for item in items] == [
        "tag", "merchant", "account", "reminder", "reminderMarker",
        "transaction", "budget",
    ]
    for item in items[:-1]:
        assert item["entity_id"] == item["entity_id"].lower()
        assert uuid.UUID(item["entity_id"]).version == 4
    assert items[2]["resolved"]["balance"] == items[2]["resolved"]["startBalance"] == 0
    assert items[2]["resolved"]["private"] is False
    assert items[2]["resolved"]["balanceCorrectionType"] == "request"
    assert items[2]["resolved"]["creditLimit"] == 0
    assert items[2]["resolved"]["enableCorrection"] is True
    assert items[3]["resolved"]["step"] == 0
    assert items[3]["resolved"]["points"] == [0]
    assert {
        "hold", "originalPayee", "mcc", "reminderMarker", "incomeBankID",
        "outcomeBankID",
    } <= items[5]["resolved"].keys()
    assert all(
        items[5]["resolved"][field] is None
        for field in (
            "hold", "originalPayee", "mcc", "reminderMarker", "incomeBankID",
            "outcomeBankID",
        )
    )
    assert not ({"qrCode", "source", "viewed"} & items[5]["resolved"].keys())
    assert items[-1]["resolved"]["tag"] == items[0]["entity_id"]


def test_account_create_verification_ignores_runtime_balance(financial_db):
    item = normalize_operations(
        financial_db,
        [{"operation": "create", "value": {
            "title": "MCP TEST Account", "type": "cash", "instrument": 1,
            "startBalance": 0,
        }}],
        "account",
        now=10,
    )[0]
    synchronized = {**item["after"], "balance": -1, "changed": 20}

    assert verify_after(item, synchronized)


def test_entity_specific_operations_do_not_accept_mixed_entity_field(financial_db):
    with pytest.raises(MutationValidationError, match="entity"):
        normalize_operations(
            financial_db,
            [
                {
                    "entity": "merchant",
                    "operation": "create",
                    "value": {"title": "MCP TEST Shop"},
                }
            ],
            entity_type="merchant",
        )


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        ({"entity": "system", "operation": "create", "value": {}}, "entity"),
        (
            {
                "entity": "account",
                "operation": "create",
                "value": {"title": "Bad", "type": "cash", "instrument": 1, "balance": 1},
            },
            "balance",
        ),
        (
            {
                "entity": "account",
                "operation": "update",
                "id": "cash",
                "set": {"startBalance": 2},
            },
            "startBalance",
        ),
        (
            {
                "entity": "account",
                "operation": "update",
                "id": "cash",
                "set": {"archive": False},
            },
            "archive",
        ),
        (
            {
                "entity": "merchant",
                "operation": "create",
                "value": {"title": "Bad", "future": True},
            },
            "future",
        ),
        (
            {
                "entity": "transaction",
                "operation": "update",
                "id": "transaction",
                "set": {"deleted": False},
            },
            "deleted",
        ),
        (
            {
                "entity": "transaction",
                "operation": "create",
                "value": {
                    "date": "2026-02-30",
                    "income": 0,
                    "outcome": 1,
                    "incomeAccount": "cash",
                    "outcomeAccount": "cash",
                    "incomeInstrument": 1,
                    "outcomeInstrument": 1,
                },
            },
            "date",
        ),
        (
            {
                "entity": "budget",
                "operation": "create",
                "value": {
                    "date": "2026-08-01",
                    "tag": "food",
                    "income": 0,
                    "incomeLock": False,
                    "outcome": math.nan,
                    "outcomeLock": True,
                },
            },
            "finite",
        ),
    ],
)
def test_operations_reject_unsafe_inputs(financial_db, operation, message):
    with pytest.raises(MutationValidationError, match=message):
        normalize_operations(financial_db, [operation])


def test_forward_refs_are_rejected(financial_db):
    with pytest.raises(MutationValidationError, match="unresolved"):
        normalize_operations(
            financial_db,
            [
                {
                    "entity": "tag",
                    "operation": "create",
                    "value": {"title": "Child", "parent": {"ref": "parent"}},
                },
                {
                    "entity": "tag",
                    "operation": "create",
                    "ref": "parent",
                    "value": {"title": "Parent"},
                },
            ],
        )


def test_local_tag_refs_reject_more_than_one_parent_level(financial_db):
    with pytest.raises(MutationValidationError, match="one level"):
        normalize_operations(
            financial_db,
            [
                {"entity": "tag", "operation": "create", "ref": "root",
                 "value": {"title": "Root"}},
                {"entity": "tag", "operation": "create", "ref": "child",
                 "value": {"title": "Child", "parent": {"ref": "root"}}},
                {"entity": "tag", "operation": "create",
                 "value": {"title": "Grandchild", "parent": {"ref": "child"}}},
            ],
        )


def test_wrong_type_refs_are_rejected(financial_db):
    with pytest.raises(MutationValidationError, match="type"):
        normalize_operations(
            financial_db,
            [
                {
                    "entity": "merchant",
                    "operation": "create",
                    "ref": "merchant",
                    "value": {"title": "Shop"},
                },
                {
                    "entity": "tag",
                    "operation": "create",
                    "value": {"title": "Child", "parent": {"ref": "merchant"}},
                },
            ],
        )


def test_multiple_users_require_explicit_owner_and_reject_cross_owner_refs(financial_db):
    financial_db.upsert_users(
        [{"id": 2, "login": "child", "currency": 1, "parent": 1, "changed": 1}]
    )
    financial_db.upsert_tags(
        [{"id": "other", "title": "Other", "user": 2, "changed": 1}]
    )

    with pytest.raises(MutationValidationError, match="owner_user_id"):
        normalize_operations(
            financial_db,
            [{"entity": "merchant", "operation": "create", "value": {"title": "Shop"}}],
        )

    with pytest.raises(MutationValidationError, match="owner"):
        normalize_operations(
            financial_db,
            [
                {
                    "entity": "budget",
                    "operation": "create",
                    "owner_user_id": 1,
                    "value": {
                        "date": "2026-08-01",
                        "tag": "other",
                        "income": 0,
                        "incomeLock": False,
                        "outcome": 10,
                        "outcomeLock": True,
                    },
                }
            ],
        )


def test_batch_bounds_duplicate_refs_and_duplicate_identities_fail_closed(financial_db):
    for operations in ([], [
        {"entity": "merchant", "operation": "create", "value": {"title": str(index)}}
        for index in range(101)
    ]):
        with pytest.raises(MutationValidationError, match="1 to 100"):
            normalize_operations(financial_db, operations)

    with pytest.raises(MutationValidationError, match="duplicate ref"):
        normalize_operations(
            financial_db,
            [
                {"entity": "merchant", "operation": "create", "ref": "same",
                 "value": {"title": "One"}},
                {"entity": "merchant", "operation": "create", "ref": "same",
                 "value": {"title": "Two"}},
            ],
        )

    with pytest.raises(MutationValidationError, match="duplicate entity"):
        normalize_operations(
            financial_db,
            [
                {"entity": "merchant", "operation": "update", "id": "shop",
                 "set": {"title": "One"}},
                {"entity": "merchant", "operation": "update", "id": "shop",
                 "set": {"title": "Two"}},
            ],
        )

def test_budget_uses_composite_key_for_update(financial_db):
    raw = {
        "user": 1,
        "tag": "food",
        "date": "2026-08-01",
        "income": 0,
        "incomeLock": False,
        "outcome": 100,
        "outcomeLock": True,
        "changed": 5,
    }
    financial_db.upsert_budgets([raw])

    item = normalize_operations(
        financial_db,
        [
            {
                "entity": "budget",
                "operation": "update",
                "key": {"owner_user_id": 1, "tag": "food", "date": "2026-08-01"},
                "set": {"outcome": 120},
            }
        ],
    )[0]

    assert item["entity_key"] == entity_key("budget", raw)
    assert item["expected_changed"] == 5
    assert item["before"] == {"outcome": 100}
    assert item["after"] == {"outcome": 120}


def test_total_budget_accepts_documented_zero_tag(financial_db):
    item = normalize_operations(
        financial_db,
        [
            {
                "entity": "budget",
                "operation": "create",
                "value": {
                    "date": "2026-09-01",
                    "tag": "00000000-0000-0000-0000-000000000000",
                    "income": 0,
                    "incomeLock": False,
                    "outcome": 100,
                    "outcomeLock": True,
                },
            }
        ],
    )[0]

    assert item["resolved"]["tag"] == "00000000-0000-0000-0000-000000000000"


@pytest.mark.parametrize(
    "operation",
    [
        {"entity": "account", "operation": "update", "id": "cash",
         "set": {"title": "Updated Cash"}},
        {"entity": "tag", "operation": "update", "id": "food",
         "set": {"title": "Updated Food"}},
        {"entity": "merchant", "operation": "update", "id": "shop",
         "set": {"title": "Updated Shop"}},
        {"entity": "reminder", "operation": "update", "id": "scheduled",
         "set": {"comment": "updated"}},
        {"entity": "reminderMarker", "operation": "update", "id": "marker",
         "set": {"state": "processed"}},
        {"entity": "transaction", "operation": "update", "id": "transaction",
         "set": {"comment": "updated"}},
        {"entity": "budget", "operation": "update",
         "key": {"owner_user_id": 1, "tag": "food", "date": "2026-08-01"},
         "set": {"outcome": 120}},
    ],
)
def test_update_supports_all_seven_user_entity_types(financial_db, operation):
    item = normalize_operations(financial_db, [operation])[0]

    assert item["entity_type"] == operation["entity"]
    assert item["operation"] == "update"
    assert item["before"] != item["after"]


def test_rebuild_preserves_unknown_raw_fields_and_verification_ignores_changed(financial_db):
    item = normalize_operations(
        financial_db,
        [
            {
                "entity": "merchant",
                "operation": "update",
                "id": "shop",
                "set": {"title": "Updated Shop"},
            }
        ],
        now=10,
    )[0]
    raw = financial_db.get_entity_raw("merchant", entity_key("merchant", {"id": "shop"}))
    raw["future"] = {"kept": True}

    rebuilt = rebuild_after(financial_db, item, raw)

    assert rebuilt["future"] == {"kept": True}
    assert rebuilt["title"] == "Updated Shop"
    assert verify_after(item, {**rebuilt, "changed": 999}) is True


def test_reminder_marker_delete_verifies_missing_entity(financial_db):
    item = normalize_operations(
        financial_db,
        [{"entity": "reminderMarker", "operation": "delete", "id": "marker"}],
    )[0]

    assert verify_after(item, None)


@pytest.mark.parametrize(
    ("entity", "raw", "expected"),
    [
        (
            "account",
            {"id": "delete-account", "user": 1, "type": "cash", "title": "A",
             "instrument": 1, "balance": 0, "startBalance": 0, "inBalance": True,
             "savings": False, "enableCorrection": False, "enableSMS": False,
             "archive": False, "changed": 4},
            {"archive": True},
        ),
        (
            "transaction",
            {"id": "delete-tx", "user": 1, "date": "2026-08-25", "income": 0,
             "outcome": 1, "incomeAccount": "cash", "outcomeAccount": "cash",
             "incomeInstrument": 1, "outcomeInstrument": 1, "deleted": False,
             "opIncome": 0, "opIncomeInstrument": None,
             "opOutcome": 0, "opOutcomeInstrument": None,
             "changed": 4},
            {"deleted": True},
        ),
        (
            "reminderMarker",
            {"id": "delete-marker", "user": 1, "date": "2026-08-25",
             "state": "planned", "reminder": "scheduled", "income": 0,
             "outcome": 1, "incomeAccount": "cash", "outcomeAccount": "cash",
             "incomeInstrument": 1, "outcomeInstrument": 1, "tag": None,
             "merchant": None, "payee": None, "comment": None, "notify": False,
             "changed": 4},
            {"state": "deleted"},
        ),
        (
            "budget",
            {"user": 1, "tag": "food", "date": "2026-08-01", "income": 10,
             "incomeLock": True, "outcome": 20, "outcomeLock": True, "changed": 4},
            {"income": 0, "incomeLock": False, "outcome": 0, "outcomeLock": False},
        ),
    ],
)
def test_safe_delete_rewrites_only_semantic_fields(financial_db, entity, raw, expected):
    getattr(financial_db, {
        "account": "upsert_accounts",
        "transaction": "upsert_transactions",
        "reminderMarker": "upsert_reminder_markers",
        "budget": "upsert_budgets",
    }[entity])([raw])
    operation = {"entity": entity, "operation": "delete"}
    if entity == "budget":
        operation["key"] = {
            "owner_user_id": raw["user"], "tag": raw["tag"], "date": raw["date"]
        }
    else:
        operation["id"] = raw["id"]

    item = normalize_operations(financial_db, [operation])[0]

    assert item["after"] == expected


@pytest.mark.parametrize("entity", ["tag", "merchant", "reminder"])
def test_unsupported_safe_delete_is_rejected(financial_db, entity):
    object_id = {"tag": "food", "merchant": "shop", "reminder": "missing"}[entity]
    with pytest.raises(MutationValidationError, match="not supported"):
        normalize_operations(
            financial_db,
            [{"entity": entity, "operation": "delete", "id": object_id}],
        )
