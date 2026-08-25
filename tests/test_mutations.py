from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zenmoney_mcp.hardened_database import HardenedDatabase, entity_key
from zenmoney_mcp.mutations import (
    MutationStateError,
    ProposalStore,
    execute_proposal,
    get_change_proposal,
    prepare_changes,
)


@pytest.fixture
def financial_db() -> HardenedDatabase:
    db = HardenedDatabase(":memory:")
    db.init_schema()
    db.upsert_instruments(
        [{"id": 1, "title": "Ruble", "shortTitle": "RUB", "symbol": "RUB",
          "rate": 1, "changed": 1}]
    )
    db.upsert_users(
        [{"id": 1, "login": "user", "currency": 1, "parent": None, "changed": 1}]
    )
    db.upsert_accounts(
        [{"id": "cash", "title": "Cash", "type": "cash", "instrument": 1,
          "balance": 100, "startBalance": 100, "inBalance": True,
          "savings": False, "enableCorrection": False, "enableSMS": False,
          "archive": False, "user": 1, "changed": 1}]
    )
    db.upsert_tags(
        [{"id": "food", "title": "Food", "parent": None,
          "showIncome": False, "showOutcome": True, "budgetIncome": False,
          "budgetOutcome": True, "required": None, "user": 1, "changed": 1}]
    )
    db.upsert_transactions(
        [{"id": "tx", "user": 1, "created": 1, "date": "2026-08-25",
          "income": 0, "outcome": 10, "incomeAccount": "cash",
          "outcomeAccount": "cash", "incomeInstrument": 1,
          "outcomeInstrument": 1, "tag": [], "merchant": None,
          "payee": None, "comment": None, "deleted": False,
          "changed": 10, "future": {"kept": True}}]
    )
    db.upsert_budgets(
        [{"user": 1, "tag": "food", "date": "2026-08-01", "income": 0,
          "incomeLock": False, "outcome": 100, "outcomeLock": True,
          "changed": 10}]
    )
    db.set_meta("user_entity_raw_complete", "1")
    return db


def mixed_updates():
    return [
        {"entity": "tag", "operation": "update", "id": "food",
         "set": {"title": "Dining"}},
        {"entity": "transaction", "operation": "update", "id": "tx",
         "set": {"tag": ["food"], "comment": "fixed"}},
    ]


def test_prepare_persists_generic_immutable_preview(financial_db, tmp_path):
    store = ProposalStore(tmp_path / "proposals.db")

    result = prepare_changes(financial_db, store, mixed_updates(), now=1_000)

    assert result["status"] == "prepared"
    assert result["created_at"] == 1_000
    assert result["expires_at"] == 87_400
    assert [item["entity"] for item in result["items"]] == ["tag", "transaction"]
    assert result["items"][0]["key"] == "food"
    assert result["items"][0]["changes"] == {
        "title": {"before": "Food", "after": "Dining"}
    }
    assert "future" not in str(result)
    assert get_change_proposal(store, result["proposal_id"], now=1_001) == result


def test_prepare_requires_full_generic_raw_snapshot(financial_db, tmp_path):
    financial_db.set_meta("user_entity_raw_complete", "0")
    store = ProposalStore(tmp_path / "proposals.db")

    with pytest.raises(MutationStateError, match="full sync"):
        prepare_changes(financial_db, store, mixed_updates())


def test_budget_preview_uses_typed_public_composite_key(financial_db, tmp_path):
    proposal = prepare_changes(
        financial_db,
        ProposalStore(tmp_path / "proposals.db"),
        [{"operation": "update",
          "key": {"owner_user_id": 1, "tag": "food", "date": "2026-08-01"},
          "set": {"outcome": 120}}],
        entity_type="budget",
    )

    assert proposal["items"][0]["key"] == {
        "owner_user_id": 1,
        "tag": "food",
        "date": "2026-08-01",
    }


def test_store_apply_expiry_recovery_cleanup_and_modes(financial_db, tmp_path):
    path = tmp_path / "private" / "proposals.db"
    store = ProposalStore(path)
    prepared = prepare_changes(financial_db, store, mixed_updates(), now=1)

    first = store.request_apply(prepared["proposal_id"], now=2)
    second = store.request_apply(prepared["proposal_id"], now=3)
    assert first["status"] == second["status"] == "pending"
    assert store.claim(prepared["proposal_id"], now=4)["status"] == "running"
    assert store.recover_running(now=5) == 1
    assert store.get(prepared["proposal_id"], now=5)["status"] == "needs_review"

    expiring = prepare_changes(financial_db, store, mixed_updates(), now=10)
    assert store.request_apply(expiring["proposal_id"], now=86_411)["status"] == "expired"
    store.cleanup(now=5 + 30 * 24 * 60 * 60 + 1)
    assert store.get(prepared["proposal_id"], now=5 + 30 * 24 * 60 * 60 + 1) is None

    assert path.parent.stat().st_mode & 0o777 == 0o700
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        assert candidate.stat().st_mode & 0o777 == 0o600


def test_store_archives_unreleased_transaction_schema_without_data_loss(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE proposals(id TEXT PRIMARY KEY, status TEXT);
        CREATE TABLE proposal_items(
            proposal_id TEXT,
            position INTEGER,
            transaction_id TEXT,
            expected_changed INTEGER,
            patch_json TEXT,
            before_json TEXT,
            after_json TEXT
        );
        INSERT INTO proposals VALUES ('legacy', 'prepared');
        INSERT INTO proposal_items VALUES (
            'legacy', 0, 'tx', 1, '{}', '{}', '{}'
        );
        """
    )
    conn.commit()
    conn.close()

    store = ProposalStore(path)
    tables = {
        row["name"]
        for row in store._connect().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }

    assert {"proposals", "proposal_items", "proposals_transaction_v1",
            "proposal_items_transaction_v1"} <= tables
    assert store._connect().execute(
        "SELECT id FROM proposals_transaction_v1"
    ).fetchone()["id"] == "legacy"


class SuccessfulMixedEngine:
    def __init__(self, db):
        self.db = db
        self.sync_calls = 0
        self.pushed: list[dict[str, list[dict]]] = []

    async def sync(self, force_full=False):
        assert force_full is False
        self.sync_calls += 1
        return {"status": "synced"}

    async def push_changes(self, changes):
        self.pushed.append(changes)
        methods = {
            "account": "upsert_accounts", "tag": "upsert_tags",
            "merchant": "upsert_merchants", "reminder": "upsert_reminders",
            "reminderMarker": "upsert_reminder_markers",
            "transaction": "upsert_transactions", "budget": "upsert_budgets",
        }
        for entity, items in changes.items():
            getattr(self.db, methods[entity])(items)
        return {"status": "synced"}


@pytest.mark.asyncio
async def test_mixed_executor_sends_one_request_and_verifies_all_types(
    financial_db, tmp_path
):
    store = ProposalStore(tmp_path / "proposals.db")
    prepared = prepare_changes(financial_db, store, mixed_updates(), now=90)
    engine = SuccessfulMixedEngine(financial_db)

    result = await execute_proposal(
        financial_db, engine, store, prepared["proposal_id"], now=100
    )

    assert result["status"] == "applied"
    assert engine.sync_calls == 2
    assert len(engine.pushed) == 1
    assert set(engine.pushed[0]) == {"tag", "transaction"}
    assert engine.pushed[0]["transaction"][0]["future"] == {"kept": True}
    assert engine.pushed[0]["transaction"][0]["changed"] == 100

    repeated = await execute_proposal(
        financial_db, engine, store, prepared["proposal_id"], now=101
    )
    assert repeated == result
    assert len(engine.pushed) == 1


@pytest.mark.asyncio
async def test_mixed_executor_rejects_whole_stale_batch_before_push(
    financial_db, tmp_path
):
    store = ProposalStore(tmp_path / "proposals.db")
    prepared = prepare_changes(financial_db, store, mixed_updates(), now=90)
    current = financial_db.get_entity_raw("tag", entity_key("tag", {"id": "food"}))
    financial_db.upsert_tags([{**current, "changed": 11, "title": "External"}])
    engine = SuccessfulMixedEngine(financial_db)

    result = await execute_proposal(
        financial_db, engine, store, prepared["proposal_id"], now=100
    )

    assert result["status"] == "conflicted"
    assert result["failure_code"] == "entity_changed"
    assert engine.pushed == []
    assert engine.sync_calls == 1


@pytest.mark.asyncio
async def test_create_collision_rejects_whole_proposal(financial_db, tmp_path):
    store = ProposalStore(tmp_path / "proposals.db")
    prepared = prepare_changes(
        financial_db,
        store,
        [{"entity": "merchant", "operation": "create",
          "value": {"title": "MCP TEST Merchant"}}],
        now=90,
    )
    item = store.execution_items(prepared["proposal_id"])[0]
    financial_db.upsert_merchants([item["after"]])
    engine = SuccessfulMixedEngine(financial_db)

    result = await execute_proposal(
        financial_db, engine, store, prepared["proposal_id"], now=100
    )

    assert result["status"] == "conflicted"
    assert result["failure_code"] == "create_identity_exists"
    assert engine.pushed == []


@pytest.mark.asyncio
async def test_write_transport_failure_is_not_retried(financial_db, tmp_path):
    store = ProposalStore(tmp_path / "proposals.db")
    prepared = prepare_changes(financial_db, store, mixed_updates(), now=90)

    class AmbiguousEngine(SuccessfulMixedEngine):
        async def push_changes(self, changes):
            self.pushed.append(changes)
            raise RuntimeError("sensitive upstream response")

    engine = AmbiguousEngine(financial_db)
    result = await execute_proposal(
        financial_db, engine, store, prepared["proposal_id"], now=100
    )

    assert result["status"] == "needs_review"
    assert result["failure_code"] == "write_result_unknown"
    assert len(engine.pushed) == 1
    assert "sensitive" not in str(result)


@pytest.mark.asyncio
async def test_partial_verification_requires_review(financial_db, tmp_path):
    store = ProposalStore(tmp_path / "proposals.db")
    prepared = prepare_changes(financial_db, store, mixed_updates(), now=90)

    class PartialEngine(SuccessfulMixedEngine):
        async def push_changes(self, changes):
            self.pushed.append(changes)
            self.db.upsert_tags(changes["tag"])
            return {"status": "synced"}

    result = await execute_proposal(
        financial_db,
        PartialEngine(financial_db),
        store,
        prepared["proposal_id"],
        now=100,
    )

    assert result["status"] == "needs_review"
    assert result["failure_code"] == "verification_mismatch"
    assert [item["result"] for item in result["items"]] == ["applied", "unknown"]
