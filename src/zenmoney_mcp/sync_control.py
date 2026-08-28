"""Single-flight file channel for remote synchronization requests."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


DEFAULT_CONTROL_PATH = Path("/sync-control/sync-state.json")
MAX_STATE_BYTES = 4096
_STATE_KEYS = {
    "state",
    "request_id",
    "force_full",
    "requested_at",
    "started_at",
    "finished_at",
    "failure_code",
}


class InvalidSyncState(ValueError):
    """Raised when the shared synchronization state is unsafe to use."""


def format_sync_timestamp(timestamp: int) -> str:
    """Render a stored Unix timestamp at the public UTC boundary."""
    if type(timestamp) is not int or timestamp < 0:
        raise ValueError("Invalid synchronization timestamp")
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("Invalid synchronization timestamp") from exc


def _idle_state() -> dict[str, Any]:
    return {
        "state": "idle",
        "request_id": None,
        "force_full": None,
        "requested_at": None,
        "started_at": None,
        "finished_at": None,
        "failure_code": None,
    }


def _is_timestamp(value: Any) -> bool:
    try:
        format_sync_timestamp(value)
    except ValueError:
        return False
    return True


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _STATE_KEYS:
        raise InvalidSyncState("Invalid synchronization state")

    state = value["state"]
    if state == "idle":
        if any(value[key] is not None for key in _STATE_KEYS - {"state"}):
            raise InvalidSyncState("Invalid synchronization state")
        return value
    if state not in {"pending", "running", "completed", "failed"}:
        raise InvalidSyncState("Invalid synchronization state")
    try:
        uuid.UUID(value["request_id"])
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidSyncState("Invalid synchronization request ID") from exc
    if type(value["force_full"]) is not bool or not _is_timestamp(
        value["requested_at"]
    ):
        raise InvalidSyncState("Invalid synchronization state")

    started_at = value["started_at"]
    finished_at = value["finished_at"]
    failure_code = value["failure_code"]
    if state == "pending":
        valid = started_at is None and finished_at is None and failure_code is None
    elif state == "running":
        valid = (
            _is_timestamp(started_at)
            and finished_at is None
            and failure_code is None
        )
    elif state == "completed":
        valid = (
            _is_timestamp(started_at)
            and _is_timestamp(finished_at)
            and failure_code is None
        )
    else:
        valid = (
            _is_timestamp(started_at)
            and _is_timestamp(finished_at)
            and failure_code == "sync_failed"
        )
    if not valid:
        raise InvalidSyncState("Invalid synchronization state")
    return value


def _read_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _idle_state()
    try:
        with path.open("rb") as state_file:
            raw = state_file.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise InvalidSyncState("Invalid synchronization state")
        return _validate_state(json.loads(raw))
    except InvalidSyncState:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidSyncState("Invalid synchronization state") from exc


def _write_unlocked(path: Path, state: dict[str, Any]) -> None:
    _validate_state(state)
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", text=True
        )
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, separators=(",", ":"), sort_keys=True)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def read_sync_state(path: Path = DEFAULT_CONTROL_PATH) -> dict[str, Any]:
    """Return the validated current state, or idle when no state exists."""
    path = Path(path)
    with _locked(path):
        return _read_unlocked(path)


def request_sync(
    path: Path = DEFAULT_CONTROL_PATH, force_full: bool = False
) -> dict[str, Any]:
    """Record one request, preserving an already pending or running request."""
    if type(force_full) is not bool:
        raise TypeError("force_full must be a boolean")
    path = Path(path)
    with _locked(path):
        current = _read_unlocked(path)
        if current["state"] in {"pending", "running"}:
            return {"status": "already_running", **current}
        state = {
            "state": "pending",
            "request_id": str(uuid.uuid4()),
            "force_full": force_full,
            "requested_at": int(time.time()),
            "started_at": None,
            "finished_at": None,
            "failure_code": None,
        }
        _write_unlocked(path, state)
        return {"status": "accepted", **state}


def claim_sync_request(
    path: Path = DEFAULT_CONTROL_PATH,
) -> dict[str, Any] | None:
    """Claim pending work, including work left running by a stopped worker."""
    path = Path(path)
    with _locked(path):
        state = _read_unlocked(path)
        if state["state"] not in {"pending", "running"}:
            return None
        state = {
            **state,
            "state": "running",
            "started_at": int(time.time()),
            "finished_at": None,
            "failure_code": None,
        }
        _write_unlocked(path, state)
        return state


def finish_sync_request(
    path: Path,
    request_id: str,
    succeeded: bool,
) -> dict[str, Any]:
    """Record the terminal result for the currently running request."""
    path = Path(path)
    with _locked(path):
        state = _read_unlocked(path)
        if state["state"] != "running" or state["request_id"] != request_id:
            raise InvalidSyncState("Synchronization request ID does not match")
        state = {
            **state,
            "state": "completed" if succeeded else "failed",
            "finished_at": int(time.time()),
            "failure_code": None if succeeded else "sync_failed",
        }
        _write_unlocked(path, state)
        return state
