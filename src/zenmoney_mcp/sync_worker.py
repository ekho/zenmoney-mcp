"""Credentialed ZenMoney cache synchronization commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from .hardened_database import HardenedDatabase
from .hardened_sync import HardenedSyncEngine
from .server import get_database_path

LOGGER = logging.getLogger(__name__)
DEFAULT_INTERVAL = 900


def read_secret(name: str) -> str:
    """Read a required secret from its file setting or environment setting."""
    file_name = os.environ.get(f"{name}_FILE")
    if file_name is not None:
        try:
            value = Path(file_name).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"{name} secret is required") from exc
    else:
        value = (os.environ.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} secret is required")
    return value


def parse_interval(value: str | None) -> int:
    """Return a non-negative worker interval, defaulting to fifteen minutes."""
    if value is None:
        return DEFAULT_INTERVAL
    try:
        interval = int(value)
    except ValueError as exc:
        raise ValueError(
            "ZENMONEY_SYNC_INTERVAL_SECONDS must be a non-negative integer"
        ) from exc
    if interval < 0:
        raise ValueError("ZENMONEY_SYNC_INTERVAL_SECONDS must be a non-negative integer")
    return interval


async def sync_once() -> dict[str, Any]:
    """Synchronize the configured local cache once."""
    database_path = get_database_path()
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    database_path.parent.chmod(0o700)
    database = HardenedDatabase(database_path, journal_mode="DELETE")
    try:
        database.init_schema()
        return await HardenedSyncEngine(
            database, read_secret("ZENMONEY_TOKEN")
        ).sync()
    finally:
        database.close()


async def run_worker(
    sync: Callable[[], Awaitable[Any]], interval: int, stop: asyncio.Event
) -> None:
    """Synchronize now and then wait one interval between later attempts."""
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, stop.set)

    while not stop.is_set():
        try:
            await sync()
        except Exception:
            LOGGER.warning(json.dumps({"event": "sync", "status": "failed"}))
        else:
            LOGGER.warning(json.dumps({"event": "sync", "status": "synced"}))

        if interval == 0:
            return
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


def _emit(status: str) -> None:
    print(json.dumps({"event": "sync", "status": status}), file=sys.stderr)


def sync_once_main() -> None:
    """Run one synchronization attempt."""
    try:
        asyncio.run(sync_once())
    except Exception:
        _emit("failed")
        raise SystemExit(1) from None
    _emit("synced")


def main() -> None:
    """Run the periodic synchronizer."""
    parser = argparse.ArgumentParser()
    parser.parse_args()
    try:
        interval = parse_interval(os.environ.get("ZENMONEY_SYNC_INTERVAL_SECONDS"))
        read_secret("ZENMONEY_TOKEN")
        asyncio.run(run_worker(sync_once, interval, asyncio.Event()))
    except ValueError:
        _emit("failed")
        raise SystemExit(1) from None
