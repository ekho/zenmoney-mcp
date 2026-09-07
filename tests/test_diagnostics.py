from __future__ import annotations

import json
import logging

import pytest

from zenmoney_mcp.hardened_database import HardenedDatabase
from zenmoney_mcp.hardened_sync import HardenedSyncEngine, SyncError


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["income", "payee"])
async def test_api_validation_identifies_field_and_item_without_values(monkeypatch, field):
    import httpx

    real_client = httpx.AsyncClient
    def handler(request):
        return httpx.Response(400, json={"error": {
            "code": "validationError",
            "message": f'Invalid property "{field}" in object Reminder fixture-secret-id. Wrong value',
        }})
    monkeypatch.setattr("zenmoney_mcp.hardened_sync.httpx.AsyncClient",
                        lambda: real_client(transport=httpx.MockTransport(handler)))
    db = HardenedDatabase(":memory:")
    with pytest.raises(SyncError) as error:
        await HardenedSyncEngine(db, "fixture-secret-token")._post_diff(
            {"reminder": [{"id": "other"}, {"id": "fixture-secret-id"}]}, 60, 1
        )
    assert error.value.diagnostics["api_error"] == {
        "entity": "reminder", "field": field, "entity_index": 1, "reason": "wrong_value",
    }
    assert "fixture-secret" not in json.dumps(error.value.diagnostics)
    db.close()


def test_exception_trace_omits_messages_locals_and_source_text():
    from zenmoney_mcp.diagnostics import exception_details

    def failing_function():
        sensitive_value = "fixture-secret-value"
        raise KeyError(sensitive_value)
    try:
        try:
            failing_function()
        except KeyError as cause:
            raise SyncError("fixture-secret-response", diagnostics={"phase": "apply_response"}) from cause
    except SyncError as error:
        details = exception_details(error)
    assert details["phase"] == "apply_response"
    assert [item["type"] for item in details["exceptions"]] == ["SyncError", "KeyError"]
    assert details["exceptions"][1]["frames"][-1]["function"] == "failing_function"
    assert details["exceptions"][1]["frames"][-1]["line"] > 0
    assert "fixture-secret" not in json.dumps(details)
    assert "/Users/" not in json.dumps(details)


def test_json_logs_have_context_and_survive_rotation(tmp_path):
    from zenmoney_mcp.diagnostics import configure_logging, emit_event

    path = tmp_path / "private-logs" / "worker.jsonl"
    handler = configure_logging(path)
    logger = logging.getLogger("zenmoney_mcp.audit")
    emit_event(logger, "audit", status="started", request_id="fixture-request")
    handler.doRollover()
    emit_event(logger, "audit", status="completed", request_id="fixture-request")
    handler.close()
    logging.getLogger("zenmoney_mcp").removeHandler(handler)
    first = json.loads(path.with_name(path.name + ".1").read_text())
    last = json.loads(path.read_text())
    assert first["status"] == "started" and last["status"] == "completed"
    assert first["request_id"] == last["request_id"] == "fixture-request"
    assert first["timestamp"].endswith("Z") and first["version"]
    assert first["pid"] > 0 and first["component"] == "zenmoney_mcp.audit"
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.with_name(path.name + ".1").stat().st_mode & 0o777 == 0o600
    reopened = configure_logging(path)
    emit_event(logger, "audit", status="reopened")
    reopened.close()
    logging.getLogger("zenmoney_mcp").removeHandler(reopened)
    assert [json.loads(line)["status"] for line in path.read_text().splitlines()] == [
        "completed", "reopened",
    ]


def test_unrecognized_upstream_errors_never_echo_untrusted_text():
    from zenmoney_mcp.hardened_sync import _api_error_details

    outputs = []
    for message in (
        'Invalid property "fixtureSecret" in object Reminder private-id. Wrong value',
        'Invalid property "income" in object FixtureSecret private-id. Wrong value',
        'Invalid property "income" in object Reminder private-id. fixtureSecret value',
        'fixtureSecret' * 4096,
    ):
        result = _api_error_details({"code": "fixtureSecretCode", "message": message}, {})
        assert result["api_code"] == "unknown"
        assert len(result["api_code_fingerprint"]) == 16
        outputs.append(result)
    assert "fixtureSecret" not in json.dumps(outputs) and "private-id" not in json.dumps(outputs)
    assert "api_error" not in outputs[0] and "api_error" not in outputs[1]
    assert outputs[2]["api_error"]["reason"] == "invalid_property"


@pytest.mark.asyncio
async def test_concurrent_operations_keep_separate_trace_callbacks():
    import asyncio
    from zenmoney_mcp.diagnostics import trace_event, trace_events

    async def operation(number):
        recorded = []
        with trace_events(lambda event, details: recorded.append(details["number"])):
            await asyncio.sleep(0)
            trace_event("checkpoint", number=number)
        return recorded

    assert await asyncio.gather(operation(1), operation(2)) == [[1], [2]]
