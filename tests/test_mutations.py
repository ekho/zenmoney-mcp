from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

import zenmoney_mcp.mutations as mutations
from zenmoney_mcp.hardened_database import HardenedDatabase, entity_key
from zenmoney_mcp.mutations import (
    MutationStateError,
    MutationValidationError,
    ProposalStore,
    execute_proposal,
    get_change_proposal,
    prepare_changes,
    prepare_recurring_payment,
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


def recurring_payment():
    return {
        "name": "Internet",
        "amount": 1200,
        "account_id": "cash",
        "category_id": "food",
        "frequency": "monthly",
        "day_of_month": 18,
        "start_date": "2026-09-18",
        "end_date": None,
        "notify": True,
    }


def test_prepare_recurring_payment_compiles_reminder_and_marker(
    financial_db, tmp_path
):
    store = ProposalStore(tmp_path / "proposals.db")

    proposal = prepare_recurring_payment(
        financial_db, store, recurring_payment(), now=1_000
    )

    assert [item["entity"] for item in proposal["items"]] == [
        "reminder",
        "reminderMarker",
    ]
    reminder, marker = [item["after"] for item in store.execution_items(proposal["proposal_id"])]
    assert reminder["interval"] == "month"
    assert reminder["step"] == 1
    assert reminder["points"] == [0]
    assert reminder["income"] == 0
    assert reminder["outcome"] == 1200
    assert reminder["incomeAccount"] == reminder["outcomeAccount"] == "cash"
    assert reminder["incomeInstrument"] == reminder["outcomeInstrument"] == 1
    assert reminder["tag"] == ["food"]
    assert reminder["payee"] == "Internet"
    assert marker["reminder"] == reminder["id"]
    assert marker["state"] == "planned"
    assert marker["date"] == "2026-09-18"
    assert marker["income"] == 0
    assert marker["outcome"] == 1200
    assert marker["incomeAccount"] == marker["outcomeAccount"] == "cash"
    assert marker["incomeInstrument"] == marker["outcomeInstrument"] == 1
    assert marker["tag"] == ["food"]
    assert marker["payee"] == "Internet"
    assert marker["notify"] is True


@pytest.mark.parametrize(
    "change",
    [
        lambda db, payment: db.upsert_accounts([
            {**db.get_entity_raw("account", entity_key("account", {"id": "cash"})), "archive": True}
        ]),
        lambda db, payment: payment.update(account_id="missing"),
        lambda db, payment: (
            db.upsert_users([{"id": 2, "login": "other", "currency": 1, "parent": None, "changed": 1}]),
            db.upsert_tags([{"id": "foreign", "title": "Foreign", "parent": None,
                             "showIncome": False, "showOutcome": True, "budgetIncome": False,
                             "budgetOutcome": True, "required": None, "user": 2, "changed": 1}]),
            payment.update(category_id="foreign"),
        ),
        lambda db, payment: payment.update(category_id="missing"),
        lambda db, payment: payment.update(frequency="weekly"),
        lambda db, payment: payment.update(day_of_month=17),
        lambda db, payment: payment.update(end_date="2026-09-17"),
    ],
    ids=[
        "archived_account",
        "missing_account",
        "foreign_category",
        "missing_category",
        "non_monthly_frequency",
        "day_mismatch",
        "end_before_start",
    ],
)
def test_prepare_recurring_payment_rejects_invalid_references_or_dates(
    financial_db, tmp_path, change
):
    payment = recurring_payment()
    change(financial_db, payment)

    with pytest.raises(MutationValidationError):
        prepare_recurring_payment(
            financial_db, ProposalStore(tmp_path / "proposals.db"), payment
        )


@pytest.mark.parametrize("field", ["user", "instrument"])
def test_prepare_recurring_payment_rejects_incomplete_account_before_compile(
    financial_db, tmp_path, monkeypatch, field
):
    account = financial_db.get_entity_raw(
        "account", entity_key("account", {"id": "cash"})
    )
    financial_db.upsert_accounts([{**account, field: None}])
    if field == "user":
        tag = financial_db.get_entity_raw("tag", entity_key("tag", {"id": "food"}))
        financial_db.upsert_tags([{**tag, "user": None}])

    monkeypatch.setattr(
        mutations,
        "prepare_changes",
        lambda *args, **kwargs: pytest.fail("invalid account reached compiler"),
    )

    with pytest.raises(MutationValidationError):
        mutations.prepare_recurring_payment(
            financial_db, ProposalStore(tmp_path / "proposals.db"), recurring_payment()
        )


@pytest.mark.parametrize(
    "amount", [float("nan"), float("inf"), float("-inf"), True],
    ids=["nan", "positive_infinity", "negative_infinity", "bool"],
)
def test_prepare_recurring_payment_rejects_nonfinite_or_bool_amount_before_compile(
    financial_db, tmp_path, monkeypatch, amount
):
    payment = recurring_payment()
    payment["amount"] = amount
    monkeypatch.setattr(
        mutations,
        "prepare_changes",
        lambda *args, **kwargs: pytest.fail("invalid amount reached compiler"),
    )

    with pytest.raises(MutationValidationError):
        mutations.prepare_recurring_payment(
            financial_db, ProposalStore(tmp_path / "proposals.db"), payment
        )


def test_prepare_recurring_payment_rejects_oversized_int_amount(
    financial_db, tmp_path
):
    payment = recurring_payment()
    payment["amount"] = 10**1000

    with pytest.raises(MutationValidationError):
        prepare_recurring_payment(
            financial_db, ProposalStore(tmp_path / "proposals.db"), payment
        )


def test_prepare_recurring_payment_rejects_signaling_decimal_amount(
    financial_db, tmp_path
):
    payment = recurring_payment()
    payment["amount"] = Decimal("sNaN")

    with pytest.raises(MutationValidationError):
        prepare_recurring_payment(
            financial_db, ProposalStore(tmp_path / "proposals.db"), payment
        )


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
        self.sync_modes: list[bool] = []
        self.pushed: list[dict[str, list[dict]]] = []

    async def sync(self, force_full=False):
        self.sync_calls += 1
        self.sync_modes.append(force_full)
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
    assert engine.sync_modes == [False, True]
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
async def test_split_executor_sends_one_idempotent_atomic_batch(financial_db, tmp_path):
    financial_db.upsert_tags(
        [{"id": "other", "title": "Other", "showIncome": False,
          "showOutcome": True, "budgetIncome": False, "budgetOutcome": True,
          "required": None, "user": 1, "changed": 1}]
    )
    raw = financial_db.get_transaction_raw("tx")
    financial_db.upsert_transactions(
        [{**raw, "outcome": 100, "outcomeBankID": "bank-operation", "changed": 10}]
    )
    store = ProposalStore(tmp_path / "proposals.db")
    prepared = prepare_changes(
        financial_db,
        store,
        [{
            "entity": "transaction",
            "operation": "split",
            "transaction_id": "tx",
            "parts": [
                {"amount": 40, "category_id": "food"},
                {"amount": "remainder", "category_id": "other"},
            ],
        }],
        now=90,
    )
    engine = SuccessfulMixedEngine(financial_db)

    result = await execute_proposal(
        financial_db, engine, store, prepared["proposal_id"], now=100
    )

    assert result["status"] == "applied"
    assert len(engine.pushed) == 1
    assert set(engine.pushed[0]) == {"transaction"}
    pushed = engine.pushed[0]["transaction"]
    assert len(pushed) == 2
    assert sum(item["outcome"] for item in pushed) == 100
    assert {item["outcomeBankID"] for item in pushed} == {"bank-operation"}
    rows = financial_db.connect().execute(
        "SELECT id,outcome FROM transactions WHERE COALESCE(deleted,0)=0"
    ).fetchall()
    assert sum(row["outcome"] for row in rows) == 100

    repeated = await execute_proposal(
        financial_db, engine, store, prepared["proposal_id"], now=101
    )
    assert repeated == result
    assert len(engine.pushed) == 1


@pytest.mark.asyncio
async def test_cross_referenced_creates_are_sent_in_one_atomic_batch(
    financial_db, tmp_path
):
    store = ProposalStore(tmp_path / "proposals.db")
    prepared = prepare_changes(
        financial_db,
        store,
        [
            {"entity": "tag", "operation": "create", "ref": "new_tag",
             "value": {"title": "MCP TEST Layered"}},
            {"entity": "transaction", "operation": "create",
             "value": {"date": "2026-08-25", "income": 0, "outcome": 1,
                       "incomeAccount": "cash", "outcomeAccount": "cash",
                       "incomeInstrument": 1, "outcomeInstrument": 1,
                       "tag": [{"ref": "new_tag"}]}},
            {"entity": "reminder", "operation": "create",
             "value": {"income": 0, "outcome": 1,
                       "incomeAccount": "cash", "outcomeAccount": "cash",
                       "incomeInstrument": 1, "outcomeInstrument": 1,
                       "tag": [{"ref": "new_tag"}],
                       "startDate": "2026-08-25"}},
        ],
        now=90,
    )
    engine = SuccessfulMixedEngine(financial_db)

    result = await execute_proposal(
        financial_db, engine, store, prepared["proposal_id"], now=100
    )

    assert result["status"] == "applied"
    assert [set(batch) for batch in engine.pushed] == [
        {"tag", "transaction", "reminder"}
    ]


@pytest.mark.asyncio
async def test_atomic_mixed_batch_failure_is_not_retried(
    financial_db, tmp_path
):
    store = ProposalStore(tmp_path / "proposals.db")
    prepared = prepare_changes(
        financial_db,
        store,
        [
            {"entity": "tag", "operation": "create", "ref": "new_tag",
             "value": {"title": "MCP TEST Layered Failure"}},
            {"entity": "transaction", "operation": "create",
             "value": {"date": "2026-08-25", "income": 0, "outcome": 1,
                       "incomeAccount": "cash", "outcomeAccount": "cash",
                       "incomeInstrument": 1, "outcomeInstrument": 1,
                       "tag": [{"ref": "new_tag"}]}},
        ],
        now=90,
    )

    class FailAtomicBatch(SuccessfulMixedEngine):
        async def push_changes(self, changes):
            self.pushed.append(changes)
            raise RuntimeError("sensitive upstream response")

    engine = FailAtomicBatch(financial_db)
    result = await execute_proposal(
        financial_db, engine, store, prepared["proposal_id"], now=100
    )

    assert result["status"] == "needs_review"
    assert result["failure_code"] == "write_result_unknown"
    assert len(engine.pushed) == 1
    assert [item["result"] for item in result["items"]] == ["unknown", "unknown"]


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
