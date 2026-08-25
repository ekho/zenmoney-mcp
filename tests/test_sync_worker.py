from __future__ import annotations

import asyncio
import json
import sys

import pytest

from zenmoney_mcp import sync_worker
from zenmoney_mcp.sync_control import read_sync_state, request_sync
from zenmoney_mcp.sync_worker import parse_interval, read_secret, run_worker, sync_once
from zenmoney_mcp.transaction_mutations import ProposalStore, prepare_transaction_changes


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
    with pytest.raises(ValueError, match="ZENMONEY_SYNC_INTERVAL_SECONDS"):
        parse_interval(value)


def test_worker_main_reads_interval_seconds_environment_variable(monkeypatch):
    observed: dict[str, object] = {}

    async def worker(sync, interval, stop, *, mutation_step=None):
        del sync, stop
        observed["interval"] = interval
        observed["mutation_step"] = mutation_step

    monkeypatch.setenv("ZENMONEY_TOKEN", "token")
    monkeypatch.setenv("ZENMONEY_SYNC_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("ZENMONEY_SYNC_INTERVAL", "-1")
    monkeypatch.setattr(sync_worker, "run_worker", worker)
    monkeypatch.setattr(sys, "argv", ["zenmoney-sync-worker"])

    sync_worker.main()

    assert observed["interval"] == 0
    assert callable(observed["mutation_step"])


def test_worker_main_rejects_invalid_interval_seconds_environment_variable(
    monkeypatch, capsys
):
    async def worker(*args):
        raise AssertionError("invalid configuration reached worker")

    monkeypatch.setenv("ZENMONEY_TOKEN", "token")
    monkeypatch.setenv("ZENMONEY_SYNC_INTERVAL_SECONDS", "-1")
    monkeypatch.setattr(sys, "argv", ["zenmoney-sync-worker"])
    monkeypatch.setattr(sync_worker, "run_worker", worker)

    with pytest.raises(SystemExit) as error:
        sync_worker.main()

    assert error.value.code == 1
    assert capsys.readouterr().err == '{"event": "sync", "status": "failed"}\n'


@pytest.mark.parametrize("token", [None, "", "   "])
def test_worker_main_rejects_missing_or_blank_environment_token_without_secret_output(
    monkeypatch, capsys, token
):
    monkeypatch.delenv("ZENMONEY_TOKEN_FILE", raising=False)
    if token is None:
        monkeypatch.delenv("ZENMONEY_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ZENMONEY_TOKEN", token)
    monkeypatch.setattr(sys, "argv", ["zenmoney-sync-worker"])

    with pytest.raises(SystemExit) as error:
        sync_worker.main()

    assert error.value.code == 1
    assert capsys.readouterr().err == '{"event": "sync", "status": "failed"}\n'


def test_worker_main_rejects_blank_token_file_without_environment_secret_output(
    monkeypatch, capsys, tmp_path
):
    token_file = tmp_path / "token"
    token_file.write_text(" \n")
    monkeypatch.setenv("ZENMONEY_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("ZENMONEY_TOKEN", "sentinel-token")
    monkeypatch.setattr(sys, "argv", ["zenmoney-sync-worker"])

    with pytest.raises(SystemExit) as error:
        sync_worker.main()

    assert error.value.code == 1
    rendered = capsys.readouterr().err
    assert "sentinel-token" not in rendered
    assert rendered == '{"event": "sync", "status": "failed"}\n'


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
            assert database.connect().execute(
                "PRAGMA journal_mode"
            ).fetchone()[0] == "delete"

        async def sync(self, force_full=False):
            assert force_full is True
            return {"status": "synced"}

    monkeypatch.setattr("zenmoney_mcp.sync_worker.HardenedSyncEngine", Engine)

    assert await sync_once(force_full=True) == {"status": "synced"}
    assert database_path.is_file()


@pytest.mark.asyncio
async def test_worker_syncs_immediately_then_exits_at_zero_interval(tmp_path):
    calls: list[bool] = []

    async def sync(force_full):
        calls.append(force_full)

    await run_worker(sync, 0, asyncio.Event(), tmp_path / "sync-state.json")

    assert calls == [False]


@pytest.mark.asyncio
async def test_worker_claims_full_request_before_scheduled_sync(tmp_path):
    control = tmp_path / "sync-state.json"
    requested = request_sync(control, force_full=True)
    calls: list[bool] = []

    async def sync(force_full):
        calls.append(force_full)

    await run_worker(sync, 0, asyncio.Event(), control)

    state = read_sync_state(control)
    assert calls == [True]
    assert state["request_id"] == requested["request_id"]
    assert state["state"] == "completed"


@pytest.mark.asyncio
async def test_worker_records_requested_sync_failure(tmp_path):
    control = tmp_path / "sync-state.json"
    request_sync(control, force_full=False)

    async def sync(force_full):
        assert force_full is False
        raise RuntimeError("sensitive response")

    await run_worker(sync, 0, asyncio.Event(), control)

    assert read_sync_state(control)["failure_code"] == "sync_failed"


@pytest.mark.asyncio
async def test_worker_picks_up_request_during_interval_wait(
    monkeypatch, tmp_path
):
    control = tmp_path / "sync-state.json"
    initial_sync_finished = asyncio.Event()
    stop = asyncio.Event()
    calls: list[bool] = []

    async def sync(force_full):
        calls.append(force_full)
        if len(calls) == 1:
            initial_sync_finished.set()
        else:
            stop.set()

    monkeypatch.setattr(sync_worker, "CONTROL_POLL_INTERVAL", 0.01)
    worker = asyncio.create_task(run_worker(sync, 60, stop, control))
    await initial_sync_finished.wait()
    request_sync(control, force_full=True)
    await asyncio.wait_for(worker, timeout=0.5)

    assert calls == [False, True]
    assert read_sync_state(control)["state"] == "completed"


@pytest.mark.asyncio
async def test_worker_processes_mutation_without_waiting_for_scheduled_sync(tmp_path):
    stop = asyncio.Event()
    sync_calls: list[bool] = []
    mutation_calls = 0

    async def sync(force_full):
        sync_calls.append(force_full)

    async def mutation_step():
        nonlocal mutation_calls
        mutation_calls += 1
        stop.set()
        return True

    await run_worker(
        sync,
        60,
        stop,
        tmp_path / "sync-state.json",
        mutation_step=mutation_step,
    )

    assert sync_calls == [False]
    assert mutation_calls == 1


@pytest.mark.asyncio
async def test_execute_next_mutation_applies_one_pending_proposal(
    monkeypatch, tmp_path
):
    database_path = tmp_path / "zenmoney.db"
    proposal_path = tmp_path / "proposals.db"
    db = sync_worker.HardenedDatabase(database_path, journal_mode="DELETE")
    db.init_schema()
    db.upsert_instruments([{"id": 1, "rate": 1, "changed": 1}])
    db.upsert_users([{"id": 1, "currency": 1, "changed": 1}])
    db.upsert_accounts(
        [{"id": "cash", "type": "checking", "instrument": 1, "changed": 1}]
    )
    db.upsert_transactions(
        [{
            "id": "tx", "user": 1, "changed": 10, "created": 1,
            "date": "2026-08-25", "income": 0, "outcome": 10,
            "incomeAccount": "cash", "outcomeAccount": "cash",
            "incomeInstrument": 1, "outcomeInstrument": 1,
            "tag": [], "deleted": False,
        }]
    )
    db.set_meta("transaction_raw_complete", "1")
    store = ProposalStore(proposal_path)
    prepared = prepare_transaction_changes(
        db,
        store,
        [{"transaction_id": "tx", "set": {"comment": "fixed"}}],
    )
    store.request_apply(prepared["proposal_id"])
    store.close()
    db.close()

    class Engine:
        def __init__(self, database, token):
            assert token == "token"
            self.db = database

        async def sync(self, force_full=False):
            return {"status": "synced"}

        async def push_transactions(self, transactions):
            self.db.upsert_transactions(transactions)
            return {"status": "synced"}

    monkeypatch.setenv("ZENMONEY_TOKEN", "token")
    monkeypatch.delenv("ZENMONEY_TOKEN_FILE", raising=False)
    monkeypatch.setattr(sync_worker, "get_database_path", lambda: database_path)
    monkeypatch.setattr(sync_worker, "HardenedSyncEngine", Engine)

    assert await sync_worker.execute_next_mutation(proposal_path) is True
    verified = ProposalStore(proposal_path)
    assert verified.get(prepared["proposal_id"])["status"] == "applied"
    verified.close()


@pytest.mark.asyncio
async def test_execute_next_mutation_does_not_replay_leftover_running_proposal(
    monkeypatch, tmp_path
):
    database_path = tmp_path / "zenmoney.db"
    proposal_path = tmp_path / "proposals.db"
    db = sync_worker.HardenedDatabase(database_path, journal_mode="DELETE")
    db.init_schema()
    store = ProposalStore(proposal_path)
    proposal_id = store.create(
        [{
            "transaction_id": "tx",
            "expected_changed": 1,
            "patch": {"comment": "fixed"},
            "before": {"comment": None},
            "after": {"comment": "fixed"},
        }]
    )
    store.request_apply(proposal_id)
    store.claim(proposal_id)
    store.close()
    db.close()
    monkeypatch.setenv("ZENMONEY_TOKEN", "token")
    monkeypatch.setattr(sync_worker, "get_database_path", lambda: database_path)

    assert await sync_worker.execute_next_mutation(proposal_path) is False
    verified = ProposalStore(proposal_path)
    assert verified.get(proposal_id)["status"] == "needs_review"
    verified.close()


@pytest.mark.asyncio
async def test_worker_waits_an_interval_before_next_sync(tmp_path):
    calls: list[float] = []
    stop = asyncio.Event()

    async def sync(force_full):
        assert force_full is False
        calls.append(asyncio.get_running_loop().time())
        if len(calls) == 2:
            stop.set()

    await run_worker(sync, 0.01, stop, tmp_path / "sync-state.json")

    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.009


@pytest.mark.asyncio
async def test_worker_stops_during_wait_without_a_second_sync(tmp_path):
    calls: list[str] = []
    stop = asyncio.Event()

    async def sync(force_full):
        assert force_full is False
        calls.append("sync")
        stop.set()

    await run_worker(sync, 60, stop, tmp_path / "sync-state.json")

    assert calls == ["sync"]


@pytest.mark.asyncio
async def test_worker_waits_before_retry_and_does_not_log_secrets_or_exception_text(
    caplog, tmp_path
):
    calls: list[float] = []
    stop = asyncio.Event()
    token = "sentinel-token"
    body = "sensitive response body"

    async def sync(force_full):
        assert force_full is False
        calls.append(asyncio.get_running_loop().time())
        if len(calls) == 1:
            raise RuntimeError(f"{token}: {body}")
        stop.set()

    await run_worker(sync, 0.01, stop, tmp_path / "sync-state.json")

    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.009
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in rendered
    assert body not in rendered
    assert [json.loads(record.getMessage())["status"] for record in caplog.records] == [
        "failed",
        "synced",
    ]
