from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from zenmoney_mcp import sync_control
from zenmoney_mcp.sync_control import (
    InvalidSyncState,
    claim_sync_request,
    finish_sync_request,
    read_sync_state,
    request_sync,
)


def test_format_sync_timestamp_uses_rfc3339_utc():
    """A public sync time must not inherit the host timezone."""
    assert sync_control.format_sync_timestamp(0) == "1970-01-01T00:00:00Z"
    assert (
        sync_control.format_sync_timestamp(1_700_000_000)
        == "2023-11-14T22:13:20Z"
    )


@pytest.mark.parametrize("timestamp", [-1, 10**100])
def test_format_sync_timestamp_rejects_invalid_epoch(timestamp):
    """Public timestamp formatting must fail closed outside Unix epoch bounds."""
    with pytest.raises(ValueError):
        sync_control.format_sync_timestamp(timestamp)


@pytest.mark.parametrize("timestamp", [-1, 10**100])
def test_control_state_rejects_invalid_epoch_timestamp(tmp_path, timestamp):
    """A valid-shaped control file cannot carry unrenderable public metadata."""
    path = tmp_path / "sync-state.json"
    path.write_text(
        json.dumps(
            {
                "state": "pending",
                "request_id": "00000000-0000-0000-0000-000000000001",
                "force_full": False,
                "requested_at": timestamp,
                "started_at": None,
                "finished_at": None,
                "failure_code": None,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(InvalidSyncState):
        read_sync_state(path)


def test_missing_control_state_is_idle(tmp_path):
    assert read_sync_state(tmp_path / "sync-state.json") == {
        "state": "idle",
        "request_id": None,
        "force_full": None,
        "requested_at": None,
        "started_at": None,
        "finished_at": None,
        "failure_code": None,
    }


def test_request_sync_is_single_flight(tmp_path):
    path = tmp_path / "sync-state.json"

    first = request_sync(path, force_full=True)
    second = request_sync(path, force_full=False)

    assert first["status"] == "accepted"
    assert second["status"] == "already_running"
    assert second["request_id"] == first["request_id"]
    assert read_sync_state(path)["force_full"] is True


def test_concurrent_requests_share_one_request_id(tmp_path):
    path = tmp_path / "sync-state.json"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda full: request_sync(path, full), (True, False)))

    assert {result["status"] for result in results} == {
        "accepted",
        "already_running",
    }
    assert len({result["request_id"] for result in results}) == 1


def test_claim_and_finish_sync_request(tmp_path):
    path = tmp_path / "sync-state.json"
    requested = request_sync(path, force_full=False)

    claimed = claim_sync_request(path)
    completed = finish_sync_request(
        path, requested["request_id"], succeeded=True
    )

    assert claimed is not None
    assert claimed["state"] == "running"
    assert claimed["started_at"] is not None
    assert completed["state"] == "completed"
    assert completed["finished_at"] is not None
    assert completed["failure_code"] is None


def test_restart_leftover_running_request_is_claimed_again(tmp_path):
    path = tmp_path / "sync-state.json"
    request_sync(path, force_full=True)
    first = claim_sync_request(path)

    second = claim_sync_request(path)

    assert first is not None and second is not None
    assert second["request_id"] == first["request_id"]
    assert second["force_full"] is True


def test_failed_request_has_only_fixed_failure_code(tmp_path):
    path = tmp_path / "sync-state.json"
    requested = request_sync(path, force_full=False)
    claim_sync_request(path)

    failed = finish_sync_request(path, requested["request_id"], succeeded=False)

    assert failed["state"] == "failed"
    assert failed["failure_code"] == "sync_failed"
    assert set(failed) == {
        "state",
        "request_id",
        "force_full",
        "requested_at",
        "started_at",
        "finished_at",
        "failure_code",
    }


def test_finish_rejects_wrong_request_id(tmp_path):
    path = tmp_path / "sync-state.json"
    request_sync(path, force_full=False)
    claim_sync_request(path)

    with pytest.raises(InvalidSyncState, match="request ID"):
        finish_sync_request(path, "00000000-0000-0000-0000-000000000000", True)


@pytest.mark.parametrize(
    "content",
    [
        '{"state":"running","token":"secret"}',
        "not-json",
        "x" * 4097,
    ],
)
def test_invalid_state_is_never_claimed(tmp_path, content):
    path = tmp_path / "sync-state.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(InvalidSyncState):
        claim_sync_request(path)
