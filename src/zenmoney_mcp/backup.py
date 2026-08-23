"""Offline-safe SQLite backup command for the ZenMoney cache."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from .hardened_database import HardenedDatabase, validate_snapshot
from .server import get_database_path


def backup_database(
    source_path: str | Path, destination_path: str | Path, *, force: bool = False
) -> Path:
    """Copy a live SQLite database using SQLite's online backup API."""
    source_path = Path(source_path)
    destination_path = Path(destination_path)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("backup source and destination must differ")
    if destination_path.exists() and not force:
        raise FileExistsError("backup destination already exists")

    source = HardenedDatabase(source_path, read_only=True)
    destination: HardenedDatabase | None = None
    temporary_path: Path | None = None
    try:
        if not validate_snapshot(source.connect()):
            raise ValueError("source is not a valid ZenMoney snapshot")
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination_path.parent,
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        destination = HardenedDatabase(temporary_path, journal_mode="DELETE")
        destination_conn = destination.connect()
        source.connect().backup(destination_conn)
        destination_conn.execute("PRAGMA journal_mode=DELETE")
        destination_conn.commit()
        destination.close()
        validation = HardenedDatabase(temporary_path, read_only=True)
        try:
            if not validate_snapshot(validation.connect()):
                raise ValueError("backup is not a valid ZenMoney snapshot")
        finally:
            validation.close()
        temporary_path.chmod(0o600)
        os.replace(temporary_path, destination_path)
    finally:
        if destination is not None:
            destination.close()
        source.close()
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return destination_path


def main() -> None:
    """Create a backup of the configured cache database."""
    parser = argparse.ArgumentParser()
    parser.add_argument("destination")
    parser.add_argument("--source", default=get_database_path())
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    destination = backup_database(args.source, args.destination, force=args.force)
    print(json.dumps({"status": "success", "path": str(destination)}))
