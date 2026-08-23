"""Tests for the remote server's read-only SQLite snapshot lifecycle."""

import os
import sqlite3

import pytest

from zenmoney_mcp import server
from zenmoney_mcp.hardened_database import (
    SCHEMA_VERSION,
    SYNC_ENTITY_TABLES,
    HardenedDatabase,
)


def _write_snapshot(path, value: str) -> None:
    database = HardenedDatabase(path)
    database.init_schema()
    database.set_meta("snapshot", value)
    database.close()


@pytest.fixture
def configured_database_path(monkeypatch, tmp_path):
    path = tmp_path / "remote.db"
    previous_db = server._db
    previous_sync_engine = server._sync_engine
    server._db = None
    server._sync_engine = None
    monkeypatch.setenv("ZENMONEY_DB_PATH", str(path))
    yield path
    if server._db is not None:
        server._db.close()
    server._db = previous_db
    server._sync_engine = previous_sync_engine


def test_get_database_path_uses_configured_path(configured_database_path):
    assert server.get_database_path() == configured_database_path


def test_database_rejects_unsupported_journal_mode(configured_database_path):
    with pytest.raises(ValueError, match="journal_mode must be DELETE or WAL"):
        HardenedDatabase(configured_database_path, journal_mode="TRUNCATE")


def test_get_db_creates_schema_at_configured_path(configured_database_path):
    database = server.get_db()

    assert database.db_path == str(configured_database_path)
    assert database.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sync_meta'"
    ).fetchone() is not None


def test_remote_database_queries_initialized_snapshot(configured_database_path):
    _write_snapshot(configured_database_path, "A")

    database = server.open_remote_db()

    assert database.check_ready()
    assert database.connect().execute(
        "SELECT value FROM sync_meta WHERE key = 'snapshot'"
    ).fetchone()[0] == "A"
    database.close()


def test_remote_database_rejects_writes(configured_database_path):
    _write_snapshot(configured_database_path, "A")
    database = server.open_remote_db()

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        database.connect().execute(
            "INSERT INTO sync_meta(key, value) VALUES ('snapshot', 'B')"
        )

    database.close()


def test_read_only_database_does_not_create_missing_file(configured_database_path):
    database = HardenedDatabase(configured_database_path, read_only=True)

    assert not database.check_ready()
    assert not configured_database_path.exists()


@pytest.mark.parametrize("contents", [b"not a database", b""])
def test_read_only_database_reports_malformed_or_uninitialized_file_as_not_ready(
    configured_database_path, contents: bytes
):
    configured_database_path.write_bytes(contents)
    database = HardenedDatabase(configured_database_path, read_only=True)

    assert not database.check_ready()
    database.close()


def test_read_only_database_reopens_replaced_snapshot(configured_database_path, tmp_path):
    replacement = tmp_path / "replacement.db"
    _write_snapshot(configured_database_path, "A")
    _write_snapshot(replacement, "B")

    first = HardenedDatabase(configured_database_path, read_only=True)
    assert first.connect().execute(
        "SELECT value FROM sync_meta WHERE key = 'snapshot'"
    ).fetchone()[0] == "A"
    first.close()
    os.replace(replacement, configured_database_path)

    second = HardenedDatabase(configured_database_path, read_only=True)
    assert second.connect().execute(
        "SELECT value FROM sync_meta WHERE key = 'snapshot'"
    ).fetchone()[0] == "B"
    second.close()


@pytest.mark.parametrize("missing_table", SYNC_ENTITY_TABLES)
def test_readiness_rejects_snapshot_missing_required_entity_table(
    configured_database_path, missing_table
):
    _write_snapshot(configured_database_path, "A")
    connection = sqlite3.connect(configured_database_path)
    connection.execute(f'DROP TABLE "{missing_table}"')
    connection.commit()
    connection.close()

    database = HardenedDatabase(configured_database_path, read_only=True)
    try:
        assert not database.check_ready()
    finally:
        database.close()


@pytest.mark.parametrize("version", [None, str(SCHEMA_VERSION + 1)])
def test_readiness_rejects_missing_or_incompatible_schema_version(
    configured_database_path, version
):
    _write_snapshot(configured_database_path, "A")
    connection = sqlite3.connect(configured_database_path)
    if version is None:
        connection.execute("DELETE FROM sync_meta WHERE key = 'schema_version'")
    else:
        connection.execute(
            "UPDATE sync_meta SET value = ? WHERE key = 'schema_version'", (version,)
        )
    connection.commit()
    connection.close()

    database = HardenedDatabase(configured_database_path, read_only=True)
    try:
        assert not database.check_ready()
    finally:
        database.close()


def test_readiness_rejects_incompatible_sync_meta_schema(configured_database_path):
    _write_snapshot(configured_database_path, "A")
    connection = sqlite3.connect(configured_database_path)
    connection.executescript(
        """
        ALTER TABLE sync_meta RENAME TO old_sync_meta;
        CREATE TABLE sync_meta(key TEXT, value TEXT);
        INSERT INTO sync_meta SELECT key, value FROM old_sync_meta;
        DROP TABLE old_sync_meta;
        """
    )
    connection.commit()
    connection.close()

    database = HardenedDatabase(configured_database_path, read_only=True)
    try:
        assert not database.check_ready()
    finally:
        database.close()
