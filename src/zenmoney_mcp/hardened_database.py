"""Schema and persistence hardening for the ZenMoney cache."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .database import Database

SCHEMA_VERSION = 2
SYNC_ENTITY_TABLES = (
    "instruments",
    "companies",
    "users",
    "accounts",
    "tags",
    "merchants",
    "transactions",
    "budgets",
    "reminders",
    "reminder_markers",
)
REQUIRED_SNAPSHOT_TABLES = frozenset((*SYNC_ENTITY_TABLES, "sync_meta"))


def validate_snapshot(conn: sqlite3.Connection) -> bool:
    """Return whether a connection contains the complete supported sync schema."""
    quick_check = conn.execute("PRAGMA quick_check").fetchone()
    if quick_check is None or quick_check[0] != "ok":
        return False
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if not REQUIRED_SNAPSHOT_TABLES <= tables:
        return False
    sync_meta_columns = {
        (str(row[1]), str(row[2]).upper(), int(row[5]))
        for row in conn.execute("PRAGMA table_info(sync_meta)").fetchall()
    }
    if sync_meta_columns != {("key", "TEXT", 1), ("value", "TEXT", 0)}:
        return False
    version = conn.execute(
        "SELECT value FROM sync_meta WHERE key = 'schema_version'"
    ).fetchone()
    return version is not None and str(version[0]) == str(SCHEMA_VERSION)


class CurrencyRateError(ValueError):
    """Raised when a required exchange rate is absent or unusable."""


class HardenedDatabase(Database):
    """Database subclass with idempotent migrations and strict FX lookup."""

    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if not self.read_only and self.db_path != ":memory:":
            path = Path(self.db_path)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            path.chmod(0o600)
        conn = super().connect()
        if not self.read_only and self.db_path != ":memory:":
            path = Path(self.db_path)
            for candidate in (
                path,
                path.with_name(path.name + "-wal"),
                path.with_name(path.name + "-shm"),
            ):
                if candidate.exists():
                    candidate.chmod(0o600)
        return conn

    def init_schema(self) -> None:
        if self.read_only:
            return
        super().init_schema()
        self._apply_hardening_migrations()

    def check_ready(self) -> bool:
        """Return whether this database is a readable ZenMoney snapshot."""
        try:
            return validate_snapshot(self.connect())
        except sqlite3.Error:
            return False

    def _columns(self, table: str) -> set[str]:
        return {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            for row in self.connect().execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _add_column(self, table: str, declaration: str) -> None:
        name = declaration.split()[0]
        if name not in self._columns(table):
            self.connect().execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")

    def _apply_hardening_migrations(self) -> None:
        conn = self.connect()
        self._add_column("accounts", "start_balance REAL")
        self._add_column("reminders", "points TEXT")
        self._add_column("reminders", "income_instrument INTEGER")
        self._add_column("reminders", "outcome_instrument INTEGER")
        self._add_column("reminder_markers", "income_instrument INTEGER")
        self._add_column("reminder_markers", "outcome_instrument INTEGER")
        self._add_column("budgets", "tag_key TEXT NOT NULL DEFAULT ''")

        # Collapse duplicates that were possible because NULL values in the old
        # composite primary key were not unique in SQLite.
        conn.execute(
            """
            DELETE FROM budgets
            WHERE rowid IN (
                SELECT rowid
                FROM (
                    SELECT rowid,
                           ROW_NUMBER() OVER (
                               PARTITION BY user, date, COALESCE(tag, '')
                               ORDER BY COALESCE(changed, 0) DESC, rowid DESC
                           ) AS duplicate_rank
                    FROM budgets
                ) ranked
                WHERE duplicate_rank > 1
            )
            """
        )
        conn.execute("UPDATE budgets SET tag_key = COALESCE(tag, '')")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_budgets_user_date_tag_key
            ON budgets(user, date, tag_key)
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO sync_meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()

    def require_instrument_rate(self, instrument_id: int | None) -> float:
        """Return a positive exchange rate or fail explicitly."""
        if instrument_id is None:
            raise CurrencyRateError("instrument id is required for currency conversion")
        row = self.connect().execute(
            "SELECT rate FROM instruments WHERE id = ?", (instrument_id,)
        ).fetchone()
        if row is None or row["rate"] is None or float(row["rate"]) <= 0:
            raise CurrencyRateError(
                f"missing or zero exchange rate for instrument {instrument_id}"
            )
        return float(row["rate"])

    def get_instrument_rate(self, instrument_id: int) -> float:
        """Keep legacy analytics strict when they use the original helper name."""
        return self.require_instrument_rate(instrument_id)

    def upsert_accounts(self, items: list[dict[str, Any]]) -> int:
        conn = self.connect()
        for item in items:
            existing = conn.execute(
                "SELECT start_balance FROM accounts WHERE id = ?", (item["id"],)
            ).fetchone()
            start_balance = (
                item.get("startBalance")
                if "startBalance" in item
                else existing["start_balance"] if existing else None
            )
            conn.execute(
                """
                INSERT INTO accounts(
                    id, title, type, instrument, company, balance, credit_limit,
                    in_balance, savings, archive, user, role, changed, start_balance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    type=excluded.type,
                    instrument=excluded.instrument,
                    company=excluded.company,
                    balance=excluded.balance,
                    credit_limit=excluded.credit_limit,
                    in_balance=excluded.in_balance,
                    savings=excluded.savings,
                    archive=excluded.archive,
                    user=excluded.user,
                    role=excluded.role,
                    changed=excluded.changed,
                    start_balance=excluded.start_balance
                """,
                (
                    item["id"],
                    item.get("title"),
                    item.get("type"),
                    item.get("instrument"),
                    item.get("company"),
                    item.get("balance"),
                    item.get("creditLimit"),
                    1 if item.get("inBalance", True) else 0,
                    1 if item.get("savings", False) else 0,
                    1 if item.get("archive", False) else 0,
                    item.get("user"),
                    item.get("role"),
                    item.get("changed"),
                    start_balance,
                ),
            )
        conn.commit()
        return len(items)

    def upsert_budgets(self, items: list[dict[str, Any]]) -> int:
        conn = self.connect()
        for item in items:
            tag = item.get("tag")
            conn.execute(
                """
                INSERT INTO budgets(
                    user, tag, date, income, income_lock, outcome,
                    outcome_lock, changed, tag_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user, date, tag_key) DO UPDATE SET
                    tag=excluded.tag,
                    income=excluded.income,
                    income_lock=excluded.income_lock,
                    outcome=excluded.outcome,
                    outcome_lock=excluded.outcome_lock,
                    changed=excluded.changed
                """,
                (
                    item.get("user"),
                    tag,
                    item.get("date"),
                    item.get("income"),
                    1 if item.get("incomeLock", False) else 0,
                    item.get("outcome"),
                    1 if item.get("outcomeLock", False) else 0,
                    item.get("changed"),
                    tag or "",
                ),
            )
        conn.commit()
        return len(items)

    def upsert_reminders(self, items: list[dict[str, Any]]) -> int:
        conn = self.connect()
        for item in items:
            existing = conn.execute(
                "SELECT points, income_instrument, outcome_instrument "
                "FROM reminders WHERE id = ?",
                (item["id"],),
            ).fetchone()
            tag = item.get("tag")
            tag_json = (
                json.dumps(tag if isinstance(tag, list) else [tag])
                if tag is not None
                else None
            )
            if "points" in item:
                points = item.get("points")
                points_json = json.dumps(points) if points is not None else None
            else:
                points_json = existing["points"] if existing else None
            income_instrument = (
                item.get("incomeInstrument")
                if "incomeInstrument" in item
                else existing["income_instrument"] if existing else None
            )
            outcome_instrument = (
                item.get("outcomeInstrument")
                if "outcomeInstrument" in item
                else existing["outcome_instrument"] if existing else None
            )
            conn.execute(
                """
                INSERT INTO reminders(
                    id, user, interval, step, start_date, end_date, income,
                    outcome, income_account, outcome_account, tag, merchant,
                    payee, comment, notify, changed, points,
                    income_instrument, outcome_instrument
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    user=excluded.user,
                    interval=excluded.interval,
                    step=excluded.step,
                    start_date=excluded.start_date,
                    end_date=excluded.end_date,
                    income=excluded.income,
                    outcome=excluded.outcome,
                    income_account=excluded.income_account,
                    outcome_account=excluded.outcome_account,
                    tag=excluded.tag,
                    merchant=excluded.merchant,
                    payee=excluded.payee,
                    comment=excluded.comment,
                    notify=excluded.notify,
                    changed=excluded.changed,
                    points=excluded.points,
                    income_instrument=excluded.income_instrument,
                    outcome_instrument=excluded.outcome_instrument
                """,
                (
                    item["id"],
                    item.get("user"),
                    item.get("interval"),
                    item.get("step"),
                    item.get("startDate"),
                    item.get("endDate"),
                    item.get("income"),
                    item.get("outcome"),
                    item.get("incomeAccount"),
                    item.get("outcomeAccount"),
                    tag_json,
                    item.get("merchant"),
                    item.get("payee"),
                    item.get("comment"),
                    1 if item.get("notify", False) else 0,
                    item.get("changed"),
                    points_json,
                    income_instrument,
                    outcome_instrument,
                ),
            )
        conn.commit()
        return len(items)

    def upsert_reminder_markers(self, items: list[dict[str, Any]]) -> int:
        """Persist marker-side instruments needed for trustworthy forecasts."""
        conn = self.connect()
        for item in items:
            existing = conn.execute(
                "SELECT income_instrument, outcome_instrument "
                "FROM reminder_markers WHERE id = ?",
                (item["id"],),
            ).fetchone()
            income_instrument = (
                item.get("incomeInstrument")
                if "incomeInstrument" in item
                else existing["income_instrument"] if existing else None
            )
            outcome_instrument = (
                item.get("outcomeInstrument")
                if "outcomeInstrument" in item
                else existing["outcome_instrument"] if existing else None
            )
            tag = item.get("tag")
            tag_json = (
                json.dumps(tag if isinstance(tag, list) else [tag])
                if tag is not None
                else None
            )
            conn.execute(
                """
                INSERT INTO reminder_markers(
                    id, user, reminder, date, state, income, outcome,
                    income_account, outcome_account, tag, merchant, payee,
                    comment, changed, income_instrument, outcome_instrument
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    user=excluded.user,
                    reminder=excluded.reminder,
                    date=excluded.date,
                    state=excluded.state,
                    income=excluded.income,
                    outcome=excluded.outcome,
                    income_account=excluded.income_account,
                    outcome_account=excluded.outcome_account,
                    tag=excluded.tag,
                    merchant=excluded.merchant,
                    payee=excluded.payee,
                    comment=excluded.comment,
                    changed=excluded.changed,
                    income_instrument=excluded.income_instrument,
                    outcome_instrument=excluded.outcome_instrument
                """,
                (
                    item["id"],
                    item.get("user"),
                    item.get("reminder"),
                    item.get("date"),
                    item.get("state"),
                    item.get("income"),
                    item.get("outcome"),
                    item.get("incomeAccount"),
                    item.get("outcomeAccount"),
                    tag_json,
                    item.get("merchant"),
                    item.get("payee"),
                    item.get("comment"),
                    item.get("changed"),
                    income_instrument,
                    outcome_instrument,
                ),
            )
        conn.commit()
        return len(items)
