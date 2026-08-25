"""Manual fail-closed capability gate for a dedicated ZenMoney test profile."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from zenmoney_mcp.hardened_database import HardenedDatabase
from zenmoney_mcp.hardened_sync import HardenedSyncEngine
from zenmoney_mcp.mutations import ProposalStore, execute_proposal, prepare_changes


@dataclass(frozen=True)
class LiveConfig:
    token_path: Path
    token: str
    owner_user_id: int


class UnsafeLiveConfiguration(ValueError):
    pass


def live_config(repo_root: Path | None = None) -> LiveConfig:
    """Read explicit live-test settings without accepting repository secrets."""
    raw_path = os.environ.get("ZENMONEY_TEST_TOKEN_FILE", "")
    raw_owner = os.environ.get("ZENMONEY_TEST_USER_ID", "")
    path = Path(raw_path)
    if not raw_path or not path.is_absolute() or path.is_symlink():
        raise UnsafeLiveConfiguration(
            "ZENMONEY_TEST_TOKEN_FILE must be an absolute regular file"
        )
    try:
        resolved = path.resolve(strict=True)
        mode = stat.S_IMODE(resolved.stat().st_mode)
    except OSError as exc:
        raise UnsafeLiveConfiguration(
            "ZENMONEY_TEST_TOKEN_FILE is unavailable"
        ) from exc
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    if resolved.is_relative_to(root):
        raise UnsafeLiveConfiguration(
            "ZENMONEY_TEST_TOKEN_FILE must be outside the repository"
        )
    if not resolved.is_file() or mode != 0o600:
        raise UnsafeLiveConfiguration(
            "ZENMONEY_TEST_TOKEN_FILE must be a regular file with mode 0600"
        )
    token = resolved.read_text(encoding="utf-8").strip()
    if not token:
        raise UnsafeLiveConfiguration("ZENMONEY_TEST_TOKEN_FILE must not be empty")
    try:
        owner = int(raw_owner)
    except ValueError as exc:
        raise UnsafeLiveConfiguration(
            "ZENMONEY_TEST_USER_ID must be a positive integer"
        ) from exc
    if owner <= 0 or str(owner) != raw_owner:
        raise UnsafeLiveConfiguration(
            "ZENMONEY_TEST_USER_ID must be a positive integer"
        )
    return LiveConfig(resolved, token, owner)


def validate_owner(db: HardenedDatabase, configured_owner: int) -> int:
    """Require one synchronized user matching the explicit test owner."""
    owners = [int(row["id"]) for row in db.connect().execute("SELECT id FROM users")]
    if len(owners) != 1:
        raise UnsafeLiveConfiguration(
            "live test snapshot must contain exactly one user"
        )
    if owners[0] != configured_owner:
        raise UnsafeLiveConfiguration(
            "live test owner does not match the synchronized user"
        )
    return owners[0]


def _print_proposal(label: str, result: dict[str, Any]) -> None:
    output: dict[str, Any] = {
        "proposal": label,
        "proposal_id": result["proposal_id"],
        "status": result["status"],
        "failure_code": result["failure_code"],
    }
    if label == "create":
        output["entity_ids"] = {
            item["entity"]: item["key"] for item in result["items"]
        }
    print(json.dumps(output, ensure_ascii=True, sort_keys=True))


async def _apply(
    label: str,
    db: HardenedDatabase,
    engine: HardenedSyncEngine,
    store: ProposalStore,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    proposal = prepare_changes(db, store, operations)
    result = await execute_proposal(db, engine, store, proposal["proposal_id"])
    _print_proposal(label, result)
    if result["status"] != "applied":
        raise RuntimeError("proposal_not_applied")
    return result


async def run_live() -> None:
    config = live_config()
    with tempfile.TemporaryDirectory(prefix="zenmoney-live-") as directory:
        root = Path(directory)
        db = HardenedDatabase(root / "snapshot.db")
        db.init_schema()
        store = ProposalStore(root / "proposals.db")
        engine = HardenedSyncEngine(db, config.token)
        try:
            await engine.sync(force_full=True)
            owner = validate_owner(db, config.owner_user_id)
            row = db.connect().execute(
                "SELECT currency FROM users WHERE id=?", (owner,)
            ).fetchone()
            if row is None or row["currency"] is None:
                raise UnsafeLiveConfiguration("test owner has no primary instrument")
            instrument = int(row["currency"])
            today = date.today().isoformat()
            month = date.today().replace(day=1).isoformat()
            title = f"MCP TEST {int(time.time())}"

            created = await _apply(
                "create",
                db,
                engine,
                store,
                [
                    {"entity": "tag", "operation": "create", "ref": "tag",
                     "owner_user_id": owner, "value": {"title": f"{title} Tag"}},
                    {"entity": "merchant", "operation": "create", "ref": "merchant",
                     "owner_user_id": owner,
                     "value": {"title": f"{title} Merchant"}},
                    {"entity": "account", "operation": "create", "ref": "account",
                     "owner_user_id": owner,
                     "value": {"title": f"{title} Account", "type": "cash",
                               "instrument": instrument, "startBalance": 0}},
                    {"entity": "reminder", "operation": "create", "ref": "reminder",
                     "owner_user_id": owner,
                     "value": {"incomeInstrument": instrument,
                               "incomeAccount": {"ref": "account"}, "income": 0,
                               "outcomeInstrument": instrument,
                               "outcomeAccount": {"ref": "account"}, "outcome": 1,
                               "tag": [{"ref": "tag"}],
                               "merchant": {"ref": "merchant"},
                               "startDate": today}},
                    {"entity": "reminderMarker", "operation": "create", "ref": "marker",
                     "owner_user_id": owner,
                     "value": {"incomeInstrument": instrument,
                               "incomeAccount": {"ref": "account"}, "income": 0,
                               "outcomeInstrument": instrument,
                               "outcomeAccount": {"ref": "account"}, "outcome": 1,
                               "tag": [{"ref": "tag"}],
                               "merchant": {"ref": "merchant"}, "date": today,
                               "reminder": {"ref": "reminder"}, "state": "planned"}},
                    {"entity": "transaction", "operation": "create", "ref": "transaction",
                     "owner_user_id": owner,
                     "value": {"date": today, "income": 0, "outcome": 1,
                               "incomeAccount": {"ref": "account"},
                               "outcomeAccount": {"ref": "account"},
                               "incomeInstrument": instrument,
                               "outcomeInstrument": instrument,
                               "tag": [{"ref": "tag"}],
                               "merchant": {"ref": "merchant"}}},
                    {"entity": "budget", "operation": "create",
                     "owner_user_id": owner,
                     "value": {"date": month, "tag": {"ref": "tag"},
                               "income": 0, "incomeLock": False,
                               "outcome": 1, "outcomeLock": True}},
                ],
            )
            keys = {item["entity"]: item["key"] for item in created["items"]}
            await _apply(
                "update",
                db,
                engine,
                store,
                [
                    {"entity": "account", "operation": "update", "id": keys["account"],
                     "set": {"title": f"{title} Account Updated"}},
                    {"entity": "tag", "operation": "update", "id": keys["tag"],
                     "set": {"title": f"{title} Tag Updated"}},
                    {"entity": "merchant", "operation": "update", "id": keys["merchant"],
                     "set": {"title": f"{title} Merchant Updated"}},
                    {"entity": "reminder", "operation": "update", "id": keys["reminder"],
                     "set": {"comment": "MCP TEST updated"}},
                    {"entity": "reminderMarker", "operation": "update",
                     "id": keys["reminderMarker"],
                     "set": {"comment": "MCP TEST updated"}},
                    {"entity": "transaction", "operation": "update",
                     "id": keys["transaction"],
                     "set": {"comment": "MCP TEST updated"}},
                    {"entity": "budget", "operation": "update", "key": keys["budget"],
                     "set": {"outcome": 2}},
                ],
            )
            await _apply(
                "delete",
                db,
                engine,
                store,
                [
                    {"entity": "transaction", "operation": "delete",
                     "id": keys["transaction"]},
                    {"entity": "reminderMarker", "operation": "delete",
                     "id": keys["reminderMarker"]},
                    {"entity": "budget", "operation": "delete", "key": keys["budget"]},
                    {"entity": "account", "operation": "delete", "id": keys["account"]},
                ],
            )
        finally:
            store.close()
            db.close()


def main() -> int:
    try:
        asyncio.run(run_live())
    except UnsafeLiveConfiguration:
        print('{"failure_code":"unsafe_live_configuration","status":"rejected"}')
        return 2
    except Exception:
        print('{"failure_code":"live_gate_failed","status":"failed"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
