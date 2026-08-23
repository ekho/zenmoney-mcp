"""Offline-safe SQLite backup command for the ZenMoney cache."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .hardened_database import HardenedDatabase
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
    if destination_path.exists():
        destination_path.unlink()

    source = HardenedDatabase(source_path, read_only=True)
    destination = HardenedDatabase(destination_path)
    try:
        source.connect().backup(destination.connect())
        destination.connect().commit()
    finally:
        destination.close()
        source.close()
    destination_path.chmod(0o600)
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
