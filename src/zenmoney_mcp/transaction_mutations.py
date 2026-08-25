"""Persistent two-step transaction mutation proposals."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from .hardened_database import HardenedDatabase

DEFAULT_MUTATION_PATH = Path("/sync-control/mutation-proposals.db")
MAX_PROPOSAL_ITEMS = 100
PREPARED_TTL_SECONDS = 24 * 60 * 60
TERMINAL_RETENTION_SECONDS = 30 * 24 * 60 * 60
TERMINAL_STATUSES = frozenset(
    {"applied", "conflicted", "failed", "needs_review", "expired"}
)
ITEM_RESULTS = frozenset({"applied", "unchanged", "conflicted", "unknown"})
EDITABLE_FIELDS = frozenset(
    {
        "date",
        "income",
        "outcome",
        "incomeAccount",
        "outcomeAccount",
        "incomeInstrument",
        "outcomeInstrument",
        "tag",
        "merchant",
        "payee",
        "comment",
        "opIncome",
        "opOutcome",
        "opIncomeInstrument",
        "opOutcomeInstrument",
        "latitude",
        "longitude",
        "deleted",
    }
)


class MutationValidationError(ValueError):
    """Raised when a proposed transaction result is unsafe or invalid."""


class MutationStateError(ValueError):
    """Raised when proposal or snapshot state cannot be used safely."""


def _now(value: int | None) -> int:
    return int(time.time()) if value is None else value


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _decode(value: str) -> Any:
    return json.loads(value)


class ProposalStore:
    """SQLite ledger for immutable transaction change proposals."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn: sqlite3.Connection | None = None
        self._connect()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if self.path != ":memory:":
            path = Path(self.path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.parent.chmod(0o700)
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
            path.chmod(0o600)
        self._conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _init_schema(self) -> None:
        self._connect().executescript(
            """
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                requested_at INTEGER,
                started_at INTEGER,
                finished_at INTEGER,
                failure_code TEXT
            );
            CREATE TABLE IF NOT EXISTS proposal_items (
                proposal_id TEXT NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                transaction_id TEXT NOT NULL,
                expected_changed INTEGER NOT NULL,
                patch_json TEXT NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                result TEXT,
                PRIMARY KEY (proposal_id, position),
                UNIQUE (proposal_id, transaction_id)
            );
            CREATE INDEX IF NOT EXISTS idx_proposals_status_created
            ON proposals(status, created_at);
            """
        )
        self._connect().commit()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def cleanup(self, now: int | None = None) -> None:
        timestamp = _now(now)
        cutoff = timestamp - TERMINAL_RETENTION_SECONDS
        with self._write() as conn:
            conn.execute(
                """
                UPDATE proposals
                SET status='expired', finished_at=?, failure_code='proposal_expired'
                WHERE status='prepared' AND expires_at <= ?
                """,
                (timestamp, timestamp),
            )
            placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
            conn.execute(
                f"DELETE FROM proposals WHERE status IN ({placeholders}) "
                "AND finished_at IS NOT NULL AND finished_at < ?",
                (*sorted(TERMINAL_STATUSES), cutoff),
            )

    def create(self, items: list[dict[str, Any]], now: int | None = None) -> str:
        timestamp = _now(now)
        self.cleanup(timestamp)
        proposal_id = str(uuid.uuid4())
        with self._write() as conn:
            conn.execute(
                "INSERT INTO proposals(id,status,created_at,expires_at) VALUES (?,?,?,?)",
                (
                    proposal_id,
                    "prepared",
                    timestamp,
                    timestamp + PREPARED_TTL_SECONDS,
                ),
            )
            conn.executemany(
                """
                INSERT INTO proposal_items(
                    proposal_id,position,transaction_id,expected_changed,
                    patch_json,before_json,after_json
                ) VALUES (?,?,?,?,?,?,?)
                """,
                [
                    (
                        proposal_id,
                        position,
                        item["transaction_id"],
                        item["expected_changed"],
                        _json(item["patch"]),
                        _json(item["before"]),
                        _json(item["after"]),
                    )
                    for position, item in enumerate(items)
                ],
            )
        return proposal_id

    def _public(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        item_rows = conn.execute(
            """
            SELECT transaction_id,expected_changed,before_json,after_json,result
            FROM proposal_items WHERE proposal_id=? ORDER BY position
            """,
            (row["id"],),
        ).fetchall()
        items = []
        for item in item_rows:
            before = _decode(item["before_json"])
            after = _decode(item["after_json"])
            items.append(
                {
                    "transaction_id": item["transaction_id"],
                    "expected_changed": item["expected_changed"],
                    "changes": {
                        key: {"before": before[key], "after": after[key]}
                        for key in sorted(after)
                    },
                    "result": item["result"],
                }
            )
        return {
            "proposal_id": row["id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "requested_at": row["requested_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "failure_code": row["failure_code"],
            "items": items,
        }

    def get(self, proposal_id: str, now: int | None = None) -> dict[str, Any] | None:
        try:
            uuid.UUID(proposal_id)
        except (AttributeError, TypeError, ValueError):
            return None
        self.cleanup(now)
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        return None if row is None else self._public(conn, row)

    def request_apply(
        self, proposal_id: str, now: int | None = None
    ) -> dict[str, Any]:
        timestamp = _now(now)
        self.cleanup(timestamp)
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise MutationStateError("proposal not found")
            if row["status"] == "prepared":
                conn.execute(
                    "UPDATE proposals SET status='pending',requested_at=? WHERE id=?",
                    (timestamp, proposal_id),
                )
                row = conn.execute(
                    "SELECT * FROM proposals WHERE id=?", (proposal_id,)
                ).fetchone()
            return self._public(conn, row)

    def claim(
        self, proposal_id: str | None = None, now: int | None = None
    ) -> dict[str, Any] | None:
        timestamp = _now(now)
        self.cleanup(timestamp)
        with self._write() as conn:
            if proposal_id is None:
                row = conn.execute(
                    "SELECT * FROM proposals WHERE status='pending' "
                    "ORDER BY requested_at,id LIMIT 1"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM proposals WHERE id=? AND status IN ('prepared','pending')",
                    (proposal_id,),
                ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE proposals SET status='running',started_at=? WHERE id=?",
                (timestamp, row["id"]),
            )
            row = conn.execute(
                "SELECT * FROM proposals WHERE id=?", (row["id"],)
            ).fetchone()
            return self._public(conn, row)

    def execution_items(self, proposal_id: str) -> list[dict[str, Any]]:
        return [
            {
                "transaction_id": row["transaction_id"],
                "expected_changed": row["expected_changed"],
                "patch": _decode(row["patch_json"]),
                "after": _decode(row["after_json"]),
            }
            for row in self._connect().execute(
                "SELECT * FROM proposal_items WHERE proposal_id=? ORDER BY position",
                (proposal_id,),
            ).fetchall()
        ]

    def finish(
        self,
        proposal_id: str,
        status: str,
        item_results: dict[str, str],
        failure_code: str | None,
        now: int | None = None,
    ) -> dict[str, Any]:
        if status not in TERMINAL_STATUSES - {"expired"}:
            raise MutationStateError("invalid terminal status")
        if any(value not in ITEM_RESULTS for value in item_results.values()):
            raise MutationStateError("invalid item result")
        timestamp = _now(now)
        with self._write() as conn:
            row = conn.execute(
                "SELECT status FROM proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                raise MutationStateError("proposal is not running")
            conn.execute(
                "UPDATE proposals SET status=?,finished_at=?,failure_code=? WHERE id=?",
                (status, timestamp, failure_code, proposal_id),
            )
            conn.executemany(
                "UPDATE proposal_items SET result=? "
                "WHERE proposal_id=? AND transaction_id=?",
                [
                    (result, proposal_id, transaction_id)
                    for transaction_id, result in item_results.items()
                ],
            )
            proposal = conn.execute(
                "SELECT * FROM proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            return self._public(conn, proposal)

    def recover_running(self, now: int | None = None) -> int:
        timestamp = _now(now)
        with self._write() as conn:
            ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM proposals WHERE status='running'"
                ).fetchall()
            ]
            if not ids:
                return 0
            conn.executemany(
                "UPDATE proposals SET status='needs_review',finished_at=?,"
                "failure_code='worker_restarted' WHERE id=?",
                [(timestamp, proposal_id) for proposal_id in ids],
            )
            conn.executemany(
                "UPDATE proposal_items SET result='unknown' WHERE proposal_id=?",
                [(proposal_id,) for proposal_id in ids],
            )
            return len(ids)


def _reference_exists(
    db: HardenedDatabase, table: str, object_id: str | int
) -> bool:
    return (
        db.connect()
        .execute(f"SELECT 1 FROM {table} WHERE id=?", (object_id,))
        .fetchone()
        is not None
    )


def _number(value: Any, field: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MutationValidationError(f"{field} must be a number")
    if not math.isfinite(value):
        raise MutationValidationError(f"{field} must be finite")
    if value < 0:
        raise MutationValidationError(f"{field} must be non-negative")
    return value


def validate_transaction_patch(
    db: HardenedDatabase, raw: dict[str, Any], patch: Any
) -> dict[str, Any]:
    """Return a validated full transaction with the patch applied."""
    if not isinstance(patch, dict) or not patch:
        raise MutationValidationError("set must be a non-empty object")
    forbidden = set(patch) - EDITABLE_FIELDS
    if forbidden:
        raise MutationValidationError(
            f"field {sorted(forbidden)[0]} is not editable"
        )
    if "deleted" in patch and patch["deleted"] is not True:
        raise MutationValidationError("deleted can only be set to true")

    result = {**raw, **patch}
    if raw.get("deleted") and patch.get("deleted") is True:
        raise MutationValidationError("deleted transaction cannot be restored or deleted again")

    if "date" in patch:
        try:
            parsed = date.fromisoformat(patch["date"])
        except (TypeError, ValueError) as exc:
            raise MutationValidationError("date must be a real ISO date") from exc
        if parsed.isoformat() != patch["date"]:
            raise MutationValidationError("date must be a real ISO date")

    for field in ("income", "outcome"):
        _number(result.get(field), field)
    if not result.get("deleted") and not (
        result.get("income", 0) > 0 or result.get("outcome", 0) > 0
    ):
        raise MutationValidationError("income or outcome must be positive")

    for field in ("opIncome", "opOutcome"):
        _number(result.get(field), field, nullable=True)
    for amount_field, instrument_field in (
        ("opIncome", "opIncomeInstrument"),
        ("opOutcome", "opOutcomeInstrument"),
    ):
        if (result.get(amount_field) is None) != (
            result.get(instrument_field) is None
        ):
            raise MutationValidationError(
                f"{amount_field} and {instrument_field} must be paired"
            )

    for field in ("incomeInstrument", "outcomeInstrument"):
        value = result.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise MutationValidationError(f"{field} must be an instrument id")
        if not _reference_exists(db, "instruments", value):
            raise MutationValidationError(f"{field} instrument does not exist")

    for field in ("opIncomeInstrument", "opOutcomeInstrument"):
        value = result.get(field)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not _reference_exists(db, "instruments", value)
        ):
            raise MutationValidationError(f"{field} instrument does not exist")

    for side in ("income", "outcome"):
        account_field = f"{side}Account"
        instrument_field = f"{side}Instrument"
        account_id = result.get(account_field)
        if not isinstance(account_id, str) or not account_id:
            raise MutationValidationError(f"{account_field} must be an account id")
        account = db.connect().execute(
            "SELECT type,instrument FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if account is None:
            raise MutationValidationError(f"{account_field} account does not exist")
        if account["type"] != "debt" and account["instrument"] != result.get(
            instrument_field
        ):
            raise MutationValidationError(
                f"{account_field} and {instrument_field} do not match"
            )

    tags = result.get("tag")
    if tags is not None:
        if (
            not isinstance(tags, list)
            or any(not isinstance(tag, str) or not tag for tag in tags)
            or len(tags) != len(set(tags))
        ):
            raise MutationValidationError("tag must contain unique tag ids")
        for tag in tags:
            if not _reference_exists(db, "tags", tag):
                raise MutationValidationError(f"tag {tag} does not exist")

    merchant = result.get("merchant")
    if merchant is not None and (
        not isinstance(merchant, str)
        or not merchant
        or not _reference_exists(db, "merchants", merchant)
    ):
        raise MutationValidationError("merchant does not exist")

    for field in ("payee", "comment"):
        if result.get(field) is not None and not isinstance(result[field], str):
            raise MutationValidationError(f"{field} must be a string or null")

    for field, minimum, maximum in (
        ("latitude", -90, 90),
        ("longitude", -180, 180),
    ):
        value = result.get(field)
        if value is not None:
            _number(abs(value), field)
            if value < minimum or value > maximum:
                raise MutationValidationError(f"{field} is out of range")

    return result


def prepare_transaction_changes(
    db: HardenedDatabase,
    store: ProposalStore,
    changes: Any,
    now: int | None = None,
) -> dict[str, Any]:
    """Validate and persist one immutable batch for later confirmation."""
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_PROPOSAL_ITEMS:
        raise MutationValidationError("changes must contain 1 to 100 items")
    if not db.transaction_mutations_ready():
        raise MutationStateError("a successful full sync is required")

    ids: list[str] = []
    prepared: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"transaction_id", "set"}:
            raise MutationValidationError(
                "each change requires transaction_id and set"
            )
        transaction_id = change["transaction_id"]
        if not isinstance(transaction_id, str) or not transaction_id:
            raise MutationValidationError("transaction_id must be a string")
        ids.append(transaction_id)
        if len(ids) != len(set(ids)):
            raise MutationValidationError("duplicate transaction_id")
        raw = db.get_transaction_raw(transaction_id)
        if raw is None:
            raise MutationValidationError("transaction does not have full raw data")
        result = validate_transaction_patch(db, raw, change["set"])
        before = {key: raw.get(key) for key in change["set"]}
        after = {key: result.get(key) for key in change["set"]}
        if before == after:
            raise MutationValidationError("set does not change the transaction")
        changed = raw.get("changed")
        if isinstance(changed, bool) or not isinstance(changed, int):
            raise MutationValidationError("transaction changed timestamp is invalid")
        prepared.append(
            {
                "transaction_id": transaction_id,
                "expected_changed": changed,
                "patch": change["set"],
                "before": before,
                "after": after,
            }
        )

    timestamp = _now(now)
    proposal_id = store.create(prepared, timestamp)
    proposal = store.get(proposal_id, timestamp)
    assert proposal is not None
    return proposal


def get_transaction_change_proposal(
    store: ProposalStore, proposal_id: str, now: int | None = None
) -> dict[str, Any]:
    proposal = store.get(proposal_id, now)
    if proposal is None:
        raise MutationStateError("proposal not found")
    return proposal
