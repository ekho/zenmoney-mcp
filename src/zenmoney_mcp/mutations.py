"""Persistent two-step proposals for ZenMoney user-entity changes."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .entity_changes import (
    DIFF_FIELDS,
    MutationStateError,
    MutationValidationError,
    normalize_operations,
    rebuild_after,
    verify_after,
)
from .hardened_database import HardenedDatabase

DEFAULT_MUTATION_PATH = Path("/sync-control/mutation-proposals.db")
MAX_PROPOSAL_ITEMS = 100
PREPARED_TTL_SECONDS = 24 * 60 * 60
TERMINAL_RETENTION_SECONDS = 30 * 24 * 60 * 60
TERMINAL_STATUSES = frozenset(
    {"applied", "conflicted", "failed", "needs_review", "expired"}
)
ITEM_RESULTS = frozenset({"applied", "unchanged", "conflicted", "unknown"})


def _now(value: int | None) -> int:
    return int(time.time()) if value is None else value


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _decode(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _public_key(entity_type: str, value: str) -> Any:
    key = _decode(value)
    if entity_type == "budget":
        return {
            "owner_user_id": key["user"],
            "tag": key["tag"],
            "date": key["date"],
        }
    return key


class ProposalStore:
    """Private SQLite ledger for immutable mixed change proposals."""

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
            for candidate in (self.path, f"{self.path}-wal", f"{self.path}-shm"):
                path = Path(candidate)
                if path.exists():
                    path.chmod(0o600)
        return self._conn

    def _init_schema(self) -> None:
        conn = self._connect()
        old_items = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proposal_items'"
        ).fetchone()
        if old_items is not None:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(proposal_items)")
            }
            if "transaction_id" in columns:
                conn.execute("PRAGMA foreign_keys=OFF")
                conn.execute(
                    "ALTER TABLE proposal_items RENAME TO proposal_items_transaction_v1"
                )
                conn.execute("ALTER TABLE proposals RENAME TO proposals_transaction_v1")
                conn.commit()
                conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
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
                entity_type TEXT NOT NULL,
                entity_key_json TEXT NOT NULL,
                operation TEXT NOT NULL,
                expected_changed INTEGER,
                before_json TEXT,
                after_json TEXT NOT NULL,
                result TEXT,
                PRIMARY KEY(proposal_id, position),
                UNIQUE(proposal_id, entity_type, entity_key_json)
            );
            CREATE INDEX IF NOT EXISTS idx_proposals_status_created
            ON proposals(status, created_at);
            """
        )
        conn.commit()

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
                "UPDATE proposals SET status='expired',finished_at=?,"
                "failure_code='proposal_expired' "
                "WHERE status='prepared' AND expires_at <= ?",
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
                (proposal_id, "prepared", timestamp, timestamp + PREPARED_TTL_SECONDS),
            )
            conn.executemany(
                """
                INSERT INTO proposal_items(
                    proposal_id,position,entity_type,entity_key_json,operation,
                    expected_changed,before_json,after_json
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        proposal_id,
                        position,
                        item["entity_type"],
                        item["entity_key"],
                        item["operation"],
                        item["expected_changed"],
                        _json(item["before"]) if item["before"] is not None else None,
                        _json(item["after"]),
                    )
                    for position, item in enumerate(items)
                ],
            )
        return proposal_id

    def _public(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        item_rows = conn.execute(
            "SELECT position,entity_type,entity_key_json,operation,expected_changed,"
            "before_json,after_json,result FROM proposal_items "
            "WHERE proposal_id=? ORDER BY position",
            (row["id"],),
        ).fetchall()
        items = []
        for item in item_rows:
            before = _decode(item["before_json"])
            after = _decode(item["after_json"])
            items.append(
                {
                    "entity": item["entity_type"],
                    "key": _public_key(
                        item["entity_type"], item["entity_key_json"]
                    ),
                    "operation": item["operation"],
                    "expected_changed": item["expected_changed"],
                    "changes": {
                        key: {
                            "before": None if before is None else before.get(key),
                            "after": after[key],
                        }
                        for key in sorted(after)
                        if key not in {"changed", "created", "user"}
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
        row = self._connect().execute(
            "SELECT * FROM proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        return None if row is None else self._public(self._connect(), row)

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
                    "SELECT * FROM proposals WHERE id=? "
                    "AND status IN ('prepared','pending')",
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
                "position": row["position"],
                "entity_type": row["entity_type"],
                "entity_key": row["entity_key_json"],
                "operation": row["operation"],
                "expected_changed": row["expected_changed"],
                "before": _decode(row["before_json"]),
                "after": _decode(row["after_json"]),
            }
            for row in self._connect().execute(
                "SELECT * FROM proposal_items WHERE proposal_id=? ORDER BY position",
                (proposal_id,),
            ).fetchall()
        ]

    def next_pending_id(self) -> str | None:
        row = self._connect().execute(
            "SELECT id FROM proposals WHERE status='pending' "
            "ORDER BY requested_at,id LIMIT 1"
        ).fetchone()
        return None if row is None else str(row["id"])

    def finish(
        self,
        proposal_id: str,
        status: str,
        item_results: dict[int, str],
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
                "UPDATE proposal_items SET result=? WHERE proposal_id=? AND position=?",
                [
                    (result, proposal_id, position)
                    for position, result in item_results.items()
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


def prepare_changes(
    db: HardenedDatabase,
    store: ProposalStore,
    operations: Any,
    entity_type: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Validate and store one immutable ordered proposal."""
    if not db.user_entity_mutations_ready():
        raise MutationStateError("a successful full sync is required")
    timestamp = _now(now)
    items = normalize_operations(db, operations, entity_type, timestamp)
    proposal_id = store.create(items, timestamp)
    proposal = store.get(proposal_id, timestamp)
    assert proposal is not None
    return proposal


def get_change_proposal(
    store: ProposalStore, proposal_id: str, now: int | None = None
) -> dict[str, Any]:
    proposal = store.get(proposal_id, now)
    if proposal is None:
        raise MutationStateError("proposal not found")
    return proposal


def _results(items: list[dict[str, Any]], result: str) -> dict[int, str]:
    return {item["position"]: result for item in items}


async def execute_proposal(
    db: HardenedDatabase,
    engine: Any,
    store: ProposalStore,
    proposal_id: str,
    now: int | None = None,
) -> dict[str, Any]:
    """Execute one exact proposal once, failing closed around ambiguous writes."""
    timestamp = _now(now)
    current = store.get(proposal_id, timestamp)
    if current is None:
        raise MutationStateError("proposal not found")
    if current["status"] not in {"prepared", "pending"}:
        return current

    proposal = store.claim(proposal_id, timestamp)
    if proposal is None:
        current = store.get(proposal_id, timestamp)
        if current is None:
            raise MutationStateError("proposal not found")
        return current
    items = store.execution_items(proposal_id)
    unchanged = _results(items, "unchanged")

    try:
        await engine.sync(force_full=False)
    except Exception:
        return store.finish(
            proposal_id, "failed", unchanged, "preflight_sync_failed", timestamp
        )
    if not db.user_entity_mutations_ready():
        return store.finish(
            proposal_id, "failed", unchanged, "mutation_not_ready", timestamp
        )

    raw_objects: dict[int, dict[str, Any] | None] = {}
    conflicts: dict[int, str] = {}
    collision = False
    for item in items:
        raw = db.get_entity_raw(item["entity_type"], item["entity_key"])
        raw_objects[item["position"]] = raw
        if item["operation"] == "create":
            if raw is not None:
                conflicts[item["position"]] = "conflicted"
                collision = True
        elif raw is None or raw.get("changed") != item["expected_changed"]:
            conflicts[item["position"]] = "conflicted"
    if conflicts:
        return store.finish(
            proposal_id,
            "conflicted",
            {**unchanged, **conflicts},
            "create_identity_exists" if collision else "entity_changed",
            timestamp,
        )

    outgoing: dict[str, list[dict[str, Any]]] = {}
    try:
        for item in items:
            value = rebuild_after(db, item, raw_objects[item["position"]])
            value["changed"] = timestamp
            outgoing.setdefault(DIFF_FIELDS[item["entity_type"]], []).append(value)
    except MutationValidationError:
        return store.finish(
            proposal_id, "failed", unchanged, "entity_invalid", timestamp
        )

    try:
        await engine.push_changes(outgoing)
    except Exception:
        return store.finish(
            proposal_id,
            "needs_review",
            _results(items, "unknown"),
            "write_result_unknown",
            timestamp,
        )

    try:
        await engine.sync(force_full=True)
    except Exception:
        return store.finish(
            proposal_id,
            "needs_review",
            _results(items, "unknown"),
            "verification_failed",
            timestamp,
        )

    results: dict[int, str] = {}
    for item in items:
        raw = db.get_entity_raw(item["entity_type"], item["entity_key"])
        if verify_after(item, raw):
            results[item["position"]] = "applied"
        elif (
            item["operation"] != "create"
            and raw is not None
            and raw.get("changed") != item["expected_changed"]
        ):
            results[item["position"]] = "conflicted"
        else:
            results[item["position"]] = "unknown"

    if all(result == "applied" for result in results.values()):
        return store.finish(proposal_id, "applied", results, None, timestamp)
    return store.finish(
        proposal_id,
        "needs_review",
        results,
        "verification_mismatch",
        timestamp,
    )
