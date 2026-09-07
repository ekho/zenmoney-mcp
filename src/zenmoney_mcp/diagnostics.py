"""Structured incident evidence without response bodies, secrets, or financial values."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import traceback
from datetime import datetime, timezone
from typing import Any

from . import __version__

_TRACE: ContextVar[Callable[[str, dict[str, Any]], None] | None] = ContextVar("incident_trace", default=None)


@contextmanager
def trace_events(callback: Callable[[str, dict[str, Any]], None]) -> Iterator[None]:
    """Bind I/O events to one operation without mutating a shared sync engine."""
    token = _TRACE.set(callback)
    try:
        yield
    finally:
        _TRACE.reset(token)


def trace_event(event: str, **details: Any) -> None:
    callback = _TRACE.get()
    if callback is not None:
        callback(event, details)
    else:
        emit_event(logging.getLogger("zenmoney_mcp.hardened_sync"), event, **details)


class SyncError(Exception):
    """Raised when synchronization cannot be validated or completed."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def exception_details(error: BaseException) -> dict[str, Any]:
    """Keep bounded exception types and locations, never messages, locals or source."""
    details: dict[str, Any] = {"exception_type": type(error).__name__}
    if isinstance(error, SyncError):
        details.update(error.diagnostics)
    exceptions = []
    seen = set()
    current = error
    while current is not None and id(current) not in seen and len(exceptions) < 3:
        seen.add(id(current))
        frames = [
            {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
            for frame in traceback.extract_tb(current.__traceback__)[-8:]
        ]
        exceptions.append({"type": type(current).__name__, "frames": frames})
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    details["exceptions"] = exceptions
    return details


def emit_event(logger: logging.Logger, event: str, **details: Any) -> None:
    """Callers pass only fixed codes, validated metadata, counts and durations."""
    logger.warning(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "version": __version__, "pid": os.getpid(), "component": logger.name,
        "event": event, **details,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        descriptor = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "a", encoding="utf-8")


def configure_logging(path: str | Path | None = None) -> RotatingFileHandler | None:
    """One file per process role, on the persistent control volume in Compose."""
    path = path or os.environ.get("ZENMONEY_LOG_FILE")
    if path is None:
        return None
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = _PrivateRotatingFileHandler(
        path, maxBytes=10 * 1024 * 1024, backupCount=4, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("zenmoney_mcp")
    if not logger.hasHandlers():
        logger.addHandler(logging.StreamHandler())
    logger.addHandler(handler)
    return handler
