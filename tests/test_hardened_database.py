from __future__ import annotations

import sqlite3

import pytest

from zenmoney_mcp.database import Database
from zenmoney_mcp.hardened_database import (
    CurrencyRateError,
    HardenedDatabase,
    SCHEMA_VERSION,
    entity_key,
)


def test_migration_deduplicates_nullable_budgets_and_sets_schema_version(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = Database(path)
    legacy.init_schema()
    conn = legacy.connect()
    conn.execute(
        "INSERT INTO budgets(user,tag,date,income,income_lock,outcome,outcome_lock,changed) VALUES (1,NULL,'2026-08-01',0,0,100,1,1)"
    )
    conn.execute(
        "INSERT INTO budgets(user,tag,date,income,income_lock,outcome,outcome_lock,changed) VALUES (1,NULL,'2026-08-01',0,0,200,1,2)"
    )
    conn.commit()
    legacy.close()

    db = HardenedDatabase(path)
    db.init_schema()
    rows = db.connect().execute("SELECT outcome,tag_key FROM budgets").fetchall()
    assert [(row["outcome"], row["tag_key"]) for row in rows] == [(200.0, "")]
    assert db.get_meta("schema_version") == str(SCHEMA_VERSION)


def test_strict_rate_lookup_rejects_missing_and_zero_rates():
    db = HardenedDatabase(":memory:")
    db.init_schema()
    db.connect().execute(
        "INSERT INTO instruments(id,title,short_title,symbol,rate,changed) VALUES (1,'Bad','BAD','?',0,1)"
    )
    db.connect().commit()
    with pytest.raises(CurrencyRateError, match="zero"):
        db.require_instrument_rate(1)
    with pytest.raises(CurrencyRateError, match="missing"):
        db.require_instrument_rate(999)



def test_reminder_marker_currency_columns_are_migrated_and_persisted():
    db = HardenedDatabase(":memory:")
    db.init_schema()
    db.upsert_reminder_markers([{
        "id": "marker", "user": 1, "date": "2026-08-21", "state": "planned",
        "income": 0, "outcome": 10, "incomeInstrument": 1,
        "outcomeInstrument": 2, "tag": ["food"], "changed": 1,
    }])
    row = db.connect().execute(
        "SELECT income_instrument,outcome_instrument FROM reminder_markers WHERE id='marker'"
    ).fetchone()
    assert row["income_instrument"] == 1
    assert row["outcome_instrument"] == 2


def test_migration_keeps_budget_row_with_highest_changed_even_if_inserted_first(tmp_path):
    path = tmp_path / "legacy-changed.db"
    legacy = Database(path)
    legacy.init_schema()
    conn = legacy.connect()
    conn.execute(
        "INSERT INTO budgets(user,tag,date,income,income_lock,outcome,outcome_lock,changed) "
        "VALUES (1,NULL,'2026-08-01',0,0,250,1,20)"
    )
    conn.execute(
        "INSERT INTO budgets(user,tag,date,income,income_lock,outcome,outcome_lock,changed) "
        "VALUES (1,NULL,'2026-08-01',0,0,100,1,10)"
    )
    conn.commit()
    legacy.close()

    db = HardenedDatabase(path)
    db.init_schema()

    rows = db.connect().execute("SELECT outcome,changed FROM budgets").fetchall()
    assert [(row["outcome"], row["changed"]) for row in rows] == [(250.0, 20)]


def test_legacy_rate_helper_is_strict_on_hardened_database():
    db = HardenedDatabase(":memory:")
    db.init_schema()
    db.connect().execute(
        "INSERT INTO instruments(id,title,short_title,symbol,rate,changed) "
        "VALUES (1,'Ruble','RUB','₽',1,1)"
    )
    db.connect().commit()

    assert db.get_instrument_rate(1) == 1.0
    with pytest.raises(CurrencyRateError, match="999"):
        db.get_instrument_rate(999)


def test_partial_account_diff_preserves_start_balance_when_field_is_omitted():
    db = HardenedDatabase(":memory:")
    db.init_schema()
    db.upsert_accounts(
        [
            {
                "id": "account",
                "title": "Initial",
                "type": "checking",
                "instrument": 1,
                "balance": 10,
                "startBalance": 7,
                "changed": 1,
            }
        ]
    )

    db.upsert_accounts(
        [
            {
                "id": "account",
                "title": "Updated",
                "type": "checking",
                "instrument": 1,
                "balance": 20,
                "changed": 2,
            }
        ]
    )

    row = db.connect().execute(
        "SELECT title, balance, start_balance FROM accounts WHERE id = 'account'"
    ).fetchone()
    assert (row["title"], row["balance"], row["start_balance"]) == (
        "Updated",
        20.0,
        7.0,
    )


def test_transaction_raw_json_preserves_unknown_fields_across_partial_diff():
    db = HardenedDatabase(":memory:")
    db.init_schema()
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
                "deleted": False,
                "source": "bank",
                "futureField": {"x": 1},
            }
        ]
    )

    db.upsert_transactions(
        [{"id": "tx", "changed": 11, "comment": "fixed"}]
    )

    assert db.get_transaction_raw("tx") == {
        "id": "tx",
        "user": 1,
        "changed": 11,
        "created": 1,
        "date": "2026-08-25",
        "income": 0,
        "outcome": 10,
        "incomeAccount": "cash",
        "outcomeAccount": "cash",
        "incomeInstrument": 1,
        "outcomeInstrument": 1,
        "deleted": False,
        "source": "bank",
        "futureField": {"x": 1},
        "comment": "fixed",
    }


def test_raw_user_entities_preserve_unknown_fields_across_partial_diff():
    db = HardenedDatabase(":memory:")
    db.init_schema()
    db.upsert_tags(
        [
            {
                "id": "tag",
                "user": 1,
                "changed": 1,
                "title": "Food",
                "showOutcome": True,
                "future": {"x": 1},
            }
        ]
    )

    db.upsert_tags([{"id": "tag", "changed": 2, "title": "Dining"}])

    assert db.get_entity_raw("tag", entity_key("tag", {"id": "tag"})) == {
        "id": "tag",
        "user": 1,
        "changed": 2,
        "title": "Dining",
        "showOutcome": True,
        "future": {"x": 1},
    }


def test_budget_raw_identity_is_canonical_composite_key():
    budget = {"user": 1, "date": "2026-08-01", "tag": None}

    assert entity_key("budget", budget) == (
        '{"date":"2026-08-01","tag":null,"user":1}'
    )


def test_deleting_uuid_entity_removes_its_raw_object():
    db = HardenedDatabase(":memory:")
    db.init_schema()
    db.upsert_merchants(
        [{"id": "merchant", "user": 1, "title": "Shop", "changed": 1}]
    )

    assert db.delete_by_ids("merchants", ["merchant"]) == 1
    assert db.get_entity_raw(
        "merchant", entity_key("merchant", {"id": "merchant"})
    ) is None


def test_migrated_transactions_require_full_raw_backfill(tmp_path):
    path = tmp_path / "legacy-transactions.db"
    legacy = Database(path)
    legacy.init_schema()
    legacy.upsert_transactions([{"id": "tx", "changed": 1}])
    legacy.close()

    db = HardenedDatabase(path)
    db.init_schema()

    assert "raw_json" in db._columns("transactions")
    assert db.get_entity_raw(
        "transaction", entity_key("transaction", {"id": "tx"})
    ) is None
    assert db.transaction_mutations_ready() is False


def test_migration_moves_existing_transaction_raw_json_to_generic_store(tmp_path):
    path = tmp_path / "transaction-raw.db"
    legacy = Database(path)
    legacy.init_schema()
    legacy.connect().execute("ALTER TABLE transactions ADD COLUMN raw_json TEXT")
    legacy.upsert_transactions([{"id": "tx", "changed": 1}])
    legacy.connect().execute(
        "UPDATE transactions SET raw_json=? WHERE id='tx'",
        ('{"changed":1,"future":true,"id":"tx"}',),
    )
    legacy.connect().commit()
    legacy.close()

    db = HardenedDatabase(path)
    db.init_schema()

    assert db.get_entity_raw(
        "transaction", entity_key("transaction", {"id": "tx"})
    ) == {"changed": 1, "future": True, "id": "tx"}


def test_partial_reminder_diff_preserves_schedule_and_currency_fields_when_omitted():
    db = HardenedDatabase(":memory:")
    db.init_schema()
    db.upsert_reminders(
        [
            {
                "id": "reminder",
                "payee": "Initial",
                "points": [1, 15],
                "incomeInstrument": 1,
                "outcomeInstrument": 2,
                "changed": 1,
            }
        ]
    )

    db.upsert_reminders(
        [{"id": "reminder", "payee": "Updated", "changed": 2}]
    )

    row = db.connect().execute(
        "SELECT payee, points, income_instrument, outcome_instrument "
        "FROM reminders WHERE id = 'reminder'"
    ).fetchone()
    assert row["payee"] == "Updated"
    assert row["points"] == "[1, 15]"
    assert row["income_instrument"] == 1
    assert row["outcome_instrument"] == 2


def test_partial_marker_diff_preserves_currency_fields_when_omitted():
    db = HardenedDatabase(":memory:")
    db.init_schema()
    db.upsert_reminder_markers(
        [
            {
                "id": "marker",
                "state": "planned",
                "incomeInstrument": 1,
                "outcomeInstrument": 2,
                "changed": 1,
            }
        ]
    )

    db.upsert_reminder_markers(
        [{"id": "marker", "state": "processed", "changed": 2}]
    )

    row = db.connect().execute(
        "SELECT state, income_instrument, outcome_instrument "
        "FROM reminder_markers WHERE id = 'marker'"
    ).fetchone()
    assert (row["state"], row["income_instrument"], row["outcome_instrument"]) == (
        "processed",
        1,
        2,
    )


def test_file_database_uses_owner_only_permissions(tmp_path):
    path = tmp_path / "zenmoney.db"
    db = HardenedDatabase(path)
    db.init_schema()
    db.connect().execute("INSERT INTO sync_meta(key,value) VALUES ('test','1')")
    db.connect().commit()

    assert path.stat().st_mode & 0o777 == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        assert sidecar.stat().st_mode & 0o777 == 0o600
