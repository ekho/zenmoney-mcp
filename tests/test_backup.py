from __future__ import annotations

import sqlite3

import pytest

from zenmoney_mcp.backup import backup_database


def test_online_backup_copies_committed_wal_rows_and_sets_owner_permissions(tmp_path):
    source_path = tmp_path / "source.db"
    destination_path = tmp_path / "backup.db"
    source = sqlite3.connect(source_path)
    source.execute("PRAGMA journal_mode=WAL")
    source.execute("CREATE TABLE records (value TEXT)")
    source.execute("INSERT INTO records VALUES ('committed')")
    source.commit()

    try:
        assert backup_database(source_path, destination_path) == destination_path
    finally:
        source.close()

    destination = sqlite3.connect(destination_path)
    try:
        assert destination.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert destination.execute("SELECT value FROM records").fetchall() == [
            ("committed",)
        ]
    finally:
        destination.close()
    assert destination_path.stat().st_mode & 0o777 == 0o600


def test_backup_rejects_existing_destination_without_force(tmp_path):
    source_path = tmp_path / "source.db"
    destination_path = tmp_path / "backup.db"
    sqlite3.connect(source_path).close()
    destination_path.write_text("do not overwrite")

    with pytest.raises(FileExistsError):
        backup_database(source_path, destination_path)


def test_backup_rejects_resolved_source_as_forced_destination_without_deleting_it(
    tmp_path,
):
    source_path = tmp_path / "source.db"
    source = sqlite3.connect(source_path)
    source.execute("CREATE TABLE records (value TEXT)")
    source.execute("INSERT INTO records VALUES ('keep')")
    source.commit()
    source.close()
    (tmp_path / "nested").mkdir()
    equivalent_destination = tmp_path / "nested" / ".." / "source.db"

    with pytest.raises(ValueError, match="must differ"):
        backup_database(source_path, equivalent_destination, force=True)

    source = sqlite3.connect(source_path)
    try:
        assert source.execute("SELECT value FROM records").fetchall() == [("keep",)]
    finally:
        source.close()


def test_backup_replaces_existing_destination_with_force(tmp_path):
    source_path = tmp_path / "source.db"
    destination_path = tmp_path / "backup.db"
    source = sqlite3.connect(source_path)
    source.execute("CREATE TABLE records (value TEXT)")
    source.execute("INSERT INTO records VALUES ('new')")
    source.commit()
    source.close()
    destination_path.write_text("old")

    backup_database(source_path, destination_path, force=True)

    destination = sqlite3.connect(destination_path)
    try:
        assert destination.execute("SELECT value FROM records").fetchall() == [("new",)]
    finally:
        destination.close()
