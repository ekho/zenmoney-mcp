from __future__ import annotations

import math
import os

import pytest

from zenmoney_mcp.hardened_database import HardenedDatabase
from zenmoney_mcp.transaction_mutations import (
    MutationStateError,
    MutationValidationError,
    ProposalStore,
    get_transaction_change_proposal,
    prepare_transaction_changes,
)


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
        [{"id": 1, "login": "user", "currency": 1, "changed": 1}]
    )
    db.upsert_accounts(
        [
            {
                "id": "cash",
                "title": "Cash",
                "type": "checking",
                "instrument": 1,
                "balance": 100,
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
                "user": 1,
                "changed": 1,
            }
        ]
    )
    db.upsert_merchants(
        [{"id": "shop", "title": "Shop", "user": 1, "changed": 1}]
    )
    db.upsert_transactions(
        [
            {
                "id": "tx",
                "user": 1,
                "changed": 10,
                "created": 1,
                "date": "2026-08-25",
                "income": 0,
                "outcome": 10,
                "incomeAccount": "cash",
                "outcomeAccount": "cash",
                "incomeInstrument": 1,
                "outcomeInstrument": 1,
                "tag": [],
                "merchant": None,
                "payee": "Store",
                "comment": None,
                "deleted": False,
                "source": "bank",
            }
        ]
    )
    db.set_meta("transaction_raw_complete", "1")
    return db


def test_prepare_persists_immutable_bounded_preview(financial_db, tmp_path):
    store = ProposalStore(tmp_path / "proposals.db")

    result = prepare_transaction_changes(
        financial_db,
        store,
        [
            {
                "transaction_id": "tx",
                "set": {"tag": ["food"], "comment": "fixed"},
            }
        ],
        now=1_000,
    )

    assert result["status"] == "prepared"
    assert result["created_at"] == 1_000
    assert result["expires_at"] == 87_400
    assert result["items"] == [
        {
            "transaction_id": "tx",
            "expected_changed": 10,
            "changes": {
                "comment": {"before": None, "after": "fixed"},
                "tag": {"before": [], "after": ["food"]},
            },
            "result": None,
        }
    ]
    stored = get_transaction_change_proposal(
        store, result["proposal_id"], now=1_001
    )
    assert stored == result
    assert "source" not in str(result)


def test_prepare_rejects_duplicate_ids_and_forbidden_fields(financial_db, tmp_path):
    store = ProposalStore(tmp_path / "proposals.db")

    with pytest.raises(MutationValidationError, match="duplicate"):
        prepare_transaction_changes(
            financial_db,
            store,
            [
                {"transaction_id": "tx", "set": {"comment": "a"}},
                {"transaction_id": "tx", "set": {"comment": "b"}},
            ],
        )
    with pytest.raises(MutationValidationError, match="not editable"):
        prepare_transaction_changes(
            financial_db,
            store,
            [{"transaction_id": "tx", "set": {"changed": 999}}],
        )


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({}, "non-empty"),
        ({"deleted": False}, "deleted"),
        ({"date": "2026-02-30"}, "date"),
        ({"outcome": -1}, "non-negative"),
        ({"outcome": math.nan}, "finite"),
        ({"tag": ["missing"]}, "tag"),
        ({"merchant": "missing"}, "merchant"),
        ({"incomeInstrument": 999}, "instrument"),
        ({"opIncome": 10, "opIncomeInstrument": None}, "paired"),
        ({"latitude": 91}, "latitude"),
    ],
)
def test_prepare_rejects_invalid_transaction_results(
    financial_db, tmp_path, patch, message
):
    store = ProposalStore(tmp_path / "proposals.db")

    with pytest.raises(MutationValidationError, match=message):
        prepare_transaction_changes(
            financial_db,
            store,
            [{"transaction_id": "tx", "set": patch}],
        )


def test_prepare_requires_full_raw_snapshot(financial_db, tmp_path):
    financial_db.set_meta("transaction_raw_complete", "0")
    store = ProposalStore(tmp_path / "proposals.db")

    with pytest.raises(MutationStateError, match="full sync"):
        prepare_transaction_changes(
            financial_db,
            store,
            [{"transaction_id": "tx", "set": {"comment": "fixed"}}],
        )


def test_prepare_bounds_batch_before_reading_transactions(financial_db, tmp_path):
    store = ProposalStore(tmp_path / "proposals.db")

    for changes in ([], [
        {"transaction_id": f"tx-{index}", "set": {"comment": "x"}}
        for index in range(101)
    ]):
        with pytest.raises(MutationValidationError, match="1 to 100"):
            prepare_transaction_changes(financial_db, store, changes)


def test_proposal_apply_request_is_idempotent_and_expiry_is_fail_closed(
    financial_db, tmp_path
):
    store = ProposalStore(tmp_path / "proposals.db")
    prepared = prepare_transaction_changes(
        financial_db,
        store,
        [{"transaction_id": "tx", "set": {"comment": "fixed"}}],
        now=100,
    )

    first = store.request_apply(prepared["proposal_id"], now=101)
    second = store.request_apply(prepared["proposal_id"], now=102)
    assert first["status"] == second["status"] == "pending"

    other = prepare_transaction_changes(
        financial_db,
        store,
        [{"transaction_id": "tx", "set": {"comment": "later"}}],
        now=200,
    )
    expired = store.request_apply(other["proposal_id"], now=86_601)
    assert expired["status"] == "expired"
    assert expired["failure_code"] == "proposal_expired"


def test_store_recovers_running_and_removes_old_terminal_rows(financial_db, tmp_path):
    path = tmp_path / "proposals.db"
    store = ProposalStore(path)
    prepared = prepare_transaction_changes(
        financial_db,
        store,
        [{"transaction_id": "tx", "set": {"comment": "fixed"}}],
        now=1,
    )
    store.request_apply(prepared["proposal_id"], now=2)
    running = store.claim(prepared["proposal_id"], now=3)
    assert running["status"] == "running"

    assert store.recover_running(now=4) == 1
    recovered = store.get(prepared["proposal_id"], now=4)
    assert recovered["status"] == "needs_review"
    assert recovered["failure_code"] == "worker_restarted"

    store.cleanup(now=4 + 30 * 24 * 60 * 60 + 1)
    assert store.get(prepared["proposal_id"], now=4 + 30 * 24 * 60 * 60 + 1) is None
    assert os.stat(path).st_mode & 0o777 == 0o600
