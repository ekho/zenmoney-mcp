from __future__ import annotations

import sqlite3

import pytest

from zenmoney_mcp.backup import backup_database
from zenmoney_mcp.hardened_database import HardenedDatabase


def _write_snapshot(path, marker: str) -> None:
    database = HardenedDatabase(path)
    database.init_schema()
    database.set_meta("snapshot", marker)
    database.close()


def test_online_backup_copies_committed_wal_rows_and_sets_owner_permissions(tmp_path):
    source_path = tmp_path / "source.db"
    destination_path = tmp_path / "backup.db"
    _write_snapshot(source_path, "committed")
    source = sqlite3.connect(source_path)
    assert source.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    source.execute("CREATE TABLE records (value TEXT)")
    source.execute("INSERT INTO records VALUES ('committed')")
    source.commit()

    try:
        assert backup_database(source_path, destination_path) == destination_path
    finally:
        source.close()

    destination = sqlite3.connect(destination_path)
    try:
        assert destination.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert destination.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert destination.execute(
            "SELECT value FROM sync_meta WHERE key = 'snapshot'"
        ).fetchone()[0] == "committed"
        assert destination.execute("SELECT value FROM records").fetchall() == [
            ("committed",)
        ]
    finally:
        destination.close()
    assert destination_path.stat().st_mode & 0o777 == 0o600


def test_backup_rejects_existing_destination_without_force(tmp_path):
    source_path = tmp_path / "source.db"
    destination_path = tmp_path / "backup.db"
    _write_snapshot(source_path, "source")
    destination_path.write_text("do not overwrite")

    with pytest.raises(FileExistsError):
        backup_database(source_path, destination_path)


def test_backup_rejects_resolved_source_as_forced_destination_without_deleting_it(
    tmp_path,
):
    source_path = tmp_path / "source.db"
    _write_snapshot(source_path, "source")
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
    _write_snapshot(source_path, "new")
    destination_path.write_text("old")

    backup_database(source_path, destination_path, force=True)

    destination = sqlite3.connect(destination_path)
    try:
        assert destination.execute(
            "SELECT value FROM sync_meta WHERE key = 'snapshot'"
        ).fetchone()[0] == "new"
    finally:
        destination.close()


@pytest.mark.parametrize("source_kind", ["missing", "malformed"])
def test_forced_backup_failure_preserves_existing_destination_byte_for_byte(
    tmp_path, source_kind
):
    source_path = tmp_path / "source.db"
    destination_path = tmp_path / "backup.db"
    original = b"previous validated backup\x00contents"
    destination_path.write_bytes(original)
    if source_kind == "malformed":
        source_path.write_bytes(b"not a sqlite database")

    with pytest.raises(sqlite3.Error):
        backup_database(source_path, destination_path, force=True)

    assert destination_path.read_bytes() == original
    assert list(tmp_path.glob(".backup.db.*.tmp")) == []


def test_forced_backup_rejects_partial_snapshot_without_replacing_destination(
    tmp_path,
):
    source_path = tmp_path / "source.db"
    destination_path = tmp_path / "backup.db"
    original = b"previous validated backup"
    _write_snapshot(source_path, "partial")
    source = sqlite3.connect(source_path)
    source.execute("DROP TABLE transactions")
    source.commit()
    source.close()
    destination_path.write_bytes(original)

    with pytest.raises(ValueError, match="valid ZenMoney snapshot"):
        backup_database(source_path, destination_path, force=True)

    assert destination_path.read_bytes() == original
    assert list(tmp_path.glob(".backup.db.*.tmp")) == []


def test_forced_backup_publish_failure_preserves_destination_and_cleans_temp(
    monkeypatch, tmp_path
):
    source_path = tmp_path / "source.db"
    destination_path = tmp_path / "backup.db"
    original = b"previous validated backup"
    _write_snapshot(source_path, "new")
    destination_path.write_bytes(original)

    def fail_replace(source, destination):
        assert source.parent == destination.parent == tmp_path
        raise OSError("synthetic publish failure")

    monkeypatch.setattr("zenmoney_mcp.backup.os.replace", fail_replace)

    with pytest.raises(OSError, match="synthetic publish failure"):
        backup_database(source_path, destination_path, force=True)

    assert destination_path.read_bytes() == original
    assert list(tmp_path.glob(".backup.db.*.tmp")) == []
