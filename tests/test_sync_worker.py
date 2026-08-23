from __future__ import annotations

import asyncio
import json

import pytest

from zenmoney_mcp.sync_worker import parse_interval, read_secret, run_worker, sync_once


def test_read_secret_prefers_nonblank_file(monkeypatch, tmp_path):
    secret_file = tmp_path / "token"
    secret_file.write_text("file-token\n")
    monkeypatch.setenv("ZENMONEY_TOKEN", "environment-token")
    monkeypatch.setenv("ZENMONEY_TOKEN_FILE", str(secret_file))

    assert read_secret("ZENMONEY_TOKEN") == "file-token"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_read_secret_rejects_missing_or_blank_values(monkeypatch, value):
    monkeypatch.delenv("ZENMONEY_TOKEN_FILE", raising=False)
    if value is None:
        monkeypatch.delenv("ZENMONEY_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ZENMONEY_TOKEN", value)

    with pytest.raises(ValueError, match="ZENMONEY_TOKEN"):
        read_secret("ZENMONEY_TOKEN")


def test_read_secret_rejects_blank_file(monkeypatch, tmp_path):
    secret_file = tmp_path / "token"
    secret_file.write_text(" \n")
    monkeypatch.setenv("ZENMONEY_TOKEN", "environment-token")
    monkeypatch.setenv("ZENMONEY_TOKEN_FILE", str(secret_file))

    with pytest.raises(ValueError, match="ZENMONEY_TOKEN"):
        read_secret("ZENMONEY_TOKEN")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 900), ("0", 0), ("15", 15)],
)
def test_parse_interval_accepts_default_zero_and_positive_values(value, expected):
    assert parse_interval(value) == expected


@pytest.mark.parametrize("value", ["-1", "one", "1.5", ""])
def test_parse_interval_rejects_negative_and_non_integer_values(value):
    with pytest.raises(ValueError, match="ZENMONEY_SYNC_INTERVAL"):
        parse_interval(value)


@pytest.mark.asyncio
async def test_sync_once_uses_file_secret_and_creates_the_configured_cache_parent(
    monkeypatch, tmp_path
):
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n")
    database_path = tmp_path / "new" / "zenmoney.db"
    monkeypatch.setenv("ZENMONEY_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("ZENMONEY_TOKEN", "environment-token")
    monkeypatch.setattr("zenmoney_mcp.sync_worker.get_database_path", lambda: database_path)

    class Engine:
        def __init__(self, database, token):
            assert token == "file-token"
            assert database.db_path == str(database_path)

        async def sync(self):
            return {"status": "synced"}

    monkeypatch.setattr("zenmoney_mcp.sync_worker.HardenedSyncEngine", Engine)

    assert await sync_once() == {"status": "synced"}
    assert database_path.is_file()


@pytest.mark.asyncio
async def test_worker_syncs_immediately_then_exits_at_zero_interval():
    calls: list[str] = []

    async def sync():
        calls.append("sync")

    await run_worker(sync, 0, asyncio.Event())

    assert calls == ["sync"]


@pytest.mark.asyncio
async def test_worker_waits_an_interval_before_next_sync():
    calls: list[float] = []
    stop = asyncio.Event()

    async def sync():
        calls.append(asyncio.get_running_loop().time())
        if len(calls) == 2:
            stop.set()

    await run_worker(sync, 0.01, stop)

    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.009


@pytest.mark.asyncio
async def test_worker_stops_during_wait_without_a_second_sync():
    calls: list[str] = []
    stop = asyncio.Event()

    async def sync():
        calls.append("sync")
        stop.set()

    await run_worker(sync, 60, stop)

    assert calls == ["sync"]


@pytest.mark.asyncio
async def test_worker_waits_before_retry_and_does_not_log_secrets_or_exception_text(caplog):
    calls: list[float] = []
    stop = asyncio.Event()
    token = "sentinel-token"
    body = "sensitive response body"

    async def sync():
        calls.append(asyncio.get_running_loop().time())
        if len(calls) == 1:
            raise RuntimeError(f"{token}: {body}")
        stop.set()

    await run_worker(sync, 0.01, stop)

    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.009
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in rendered
    assert body not in rendered
    assert [json.loads(record.getMessage())["status"] for record in caplog.records] == [
        "failed",
        "synced",
    ]
