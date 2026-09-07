"""Validated, atomic ZenMoney synchronization."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any

import httpx

from .diagnostics import SyncError, exception_details, trace_event
from .entity_changes import DIFF_FIELDS, EDITABLE
from .hardened_database import HardenedDatabase

ZENMONEY_API_URL = "https://api.zenmoney.ru/v8/diff/"
ENTITY_MAPPING = {
    "instrument": ("upsert_instruments", "instruments"),
    "company": ("upsert_companies", "companies"),
    "user": ("upsert_users", "users"),
    "account": ("upsert_accounts", "accounts"),
    "tag": ("upsert_tags", "tags"),
    "merchant": ("upsert_merchants", "merchants"),
    "transaction": ("upsert_transactions", "transactions"),
    "budget": ("upsert_budgets", "budgets"),
    "reminder": ("upsert_reminders", "reminders"),
    "reminderMarker": ("upsert_reminder_markers", "reminder_markers"),
}


_ERROR_FIELDS = set().union(*EDITABLE.values()) | {
    "id", "user", "changed", "created", "deleted", "originalPayee", "private",
    "startBalance", "balance", "incomeBankID", "outcomeBankID",
}


def _api_error_details(error: Any, request_body: dict[str, Any]) -> dict[str, Any]:
    code = error.get("code") if isinstance(error, dict) else None
    details: dict[str, Any] = {"api_code": "validationError" if code == "validationError" else "unknown"}
    if isinstance(code, str) and code != "validationError":
        details["api_code_fingerprint"] = hashlib.sha256(code.encode()).hexdigest()[:16]
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str) or len(message) > 4096:
        return details
    # Upstream example: https://github.com/zenmoney/ZenPlugins/issues/794
    match = re.fullmatch(
        r'Invalid property "([A-Za-z][A-Za-z0-9]{0,63})" in object '
        r'(Account|Tag|Merchant|Reminder|ReminderMarker|Transaction|Budget) '
        r'([^\s.]{1,128})\. ?([^\r\n]*)', message,
    )
    if match is None or match[1] not in _ERROR_FIELDS:
        return details
    entity = match[2][0].lower() + match[2][1:]
    reason = "wrong_value" if match[4].rstrip(".") == "Wrong value" else "invalid_property"
    target: dict[str, Any] = {"entity": entity, "field": match[1], "reason": reason}
    positions = [index for index, item in enumerate(request_body.get(entity, []))
                 if isinstance(item, dict) and item.get("id") == match[3]]
    if len(positions) == 1:
        target["entity_index"] = positions[0]
    details["api_error"] = target
    return details


class HardenedSyncEngine:
    def __init__(self, db: HardenedDatabase, token: str):
        self.db = db
        self.token = token

    @staticmethod
    def _validate_diff(diff_data: Any) -> dict[str, Any]:
        if not isinstance(diff_data, dict):
            raise SyncError("ZenMoney sync response must be a JSON object")
        timestamp = diff_data.get("serverTimestamp")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp <= 0
        ):
            raise SyncError(
                "ZenMoney sync response is missing a positive serverTimestamp"
            )
        for entity in ENTITY_MAPPING:
            value = diff_data.get(entity, [])
            if value is not None and not isinstance(value, list):
                raise SyncError(f"ZenMoney sync field {entity} must be an array")
        deletions = diff_data.get("deletion", [])
        if deletions is not None and not isinstance(deletions, list):
            raise SyncError("ZenMoney sync field deletion must be an array")
        for index, deletion in enumerate(deletions or []):
            if (
                not isinstance(deletion, dict)
                or not isinstance(deletion.get("object"), str)
                or not deletion.get("object")
                or deletion.get("id") is None
            ):
                raise SyncError(
                    f"ZenMoney sync deletion[{index}] must contain object and id"
                )
        return diff_data

    def _staging_database(self, *, force_full: bool) -> HardenedDatabase:
        staging = HardenedDatabase(":memory:")
        staging.init_schema()
        if not force_full:
            self.db.connect().commit()
            self.db.connect().backup(staging.connect())
            staging._apply_hardening_migrations()
        return staging

    @staticmethod
    def _apply_to_staging(
        staging: HardenedDatabase,
        diff_data: dict[str, Any],
    ) -> dict[str, Any]:
        updated: dict[str, int] = {}
        deleted: dict[str, int] = {}
        warnings: list[str] = []
        for entity_name, (method_name, table_name) in ENTITY_MAPPING.items():
            items = diff_data.get(entity_name) or []
            if not items:
                continue
            count = getattr(staging, method_name)(items)
            if count:
                updated[table_name] = count

        for deletion in diff_data.get("deletion") or []:
            if not isinstance(deletion, dict):
                continue
            object_name = deletion.get("object")
            object_id = deletion.get("id")
            mapping = ENTITY_MAPPING.get(object_name)
            if mapping is None or object_id is None:
                continue
            _, table_name = mapping
            if table_name == "budgets":
                # Budget deletions require their composite identity; an opaque id
                # cannot safely be mapped to a row. Surface the limitation instead
                # of silently pretending the deletion was applied.
                warnings.append(
                    f"budget deletion {object_id} could not be mapped to its composite key"
                )
                continue
            count = staging.delete_by_ids(table_name, [object_id])
            if count:
                deleted[table_name] = deleted.get(table_name, 0) + count
        result: dict[str, Any] = {"updated": updated, "deleted": deleted}
        if warnings:
            result["warnings"] = warnings
        return result

    def _publish_staging(self, staging: HardenedDatabase) -> None:
        """Replace the live cache with a fully prepared staging snapshot."""
        self.db.connect().commit()
        staging.connect().backup(self.db.connect())
        self.db.connect().commit()

    def apply_diff_data(
        self,
        diff_data: dict[str, Any],
        *,
        force_full: bool = False,
        last_sync_time: int | None = None,
    ) -> dict[str, Any]:
        validated = self._validate_diff(diff_data)
        if force_full and any(
            not isinstance(validated.get(entity), list)
            for entity in ENTITY_MAPPING
        ):
            raise SyncError("full sync response is missing entity arrays")
        if not force_full and any(
            deletion.get("object") == "budget"
            for deletion in validated.get("deletion") or []
        ):
            raise SyncError(
                "incremental budget deletion cannot be mapped safely; run a full sync"
            )
        staging = self._staging_database(force_full=force_full)
        rollback = HardenedDatabase(":memory:")
        try:
            result = self._apply_to_staging(staging, validated)
            staging.set_server_timestamp(validated["serverTimestamp"])
            staging.set_meta(
                "last_sync_time",
                str(last_sync_time if last_sync_time is not None else int(time.time())),
            )
            if force_full:
                staging.set_meta("user_entity_raw_complete", "1")

            # Keep a byte-for-byte SQLite snapshot so a publication error cannot
            # leave the live cache half replaced.
            self.db.connect().commit()
            self.db.connect().backup(rollback.connect())
            try:
                self._publish_staging(staging)
            except Exception:
                rollback.connect().backup(self.db.connect())
                self.db.connect().commit()
                raise

            result.update(
                {
                    "new_server_timestamp": validated["serverTimestamp"],
                    "status": "synced",
                    "full_replacement": force_full,
                }
            )
            return result
        finally:
            staging.close()
            rollback.close()

    async def sync(self, force_full: bool = False) -> dict[str, Any]:
        started = time.monotonic()
        request_body = {
            "currentClientTimestamp": int(time.time()),
            "serverTimestamp": 0 if force_full else self.db.get_server_timestamp(),
        }
        timeout = 300.0 if force_full else 60.0
        attempts = 2 if force_full else 1
        payload = await self._post_diff(request_body, timeout, attempts)

        result = self._apply_response(payload, force_full=force_full)
        result["sync_duration_ms"] = int((time.monotonic() - started) * 1000)
        return result

    async def push_changes(
        self, changes: dict[str, list[dict[str, Any]]]
    ) -> dict[str, Any]:
        """Send one non-empty mixed user-entity change set."""
        if (
            not isinstance(changes, dict)
            or not changes
            or set(changes) - set(DIFF_FIELDS.values())
            or any(not isinstance(items, list) or not items for items in changes.values())
        ):
            raise SyncError("user-entity write batch is invalid")
        request_body = {
            "currentClientTimestamp": int(time.time()),
            "serverTimestamp": self.db.get_server_timestamp(),
            **changes,
        }
        payload = await self._post_diff(request_body, 60.0, 1)
        return self._apply_response(payload, force_full=False)

    def _apply_response(self, payload: Any, *, force_full: bool) -> dict[str, Any]:
        trace_event("apply_response_started", force_full=force_full)
        try:
            result = self.apply_diff_data(
                payload, force_full=force_full, last_sync_time=int(time.time())
            )
        except Exception as exc:
            raise SyncError(
                "Could not apply the response to the local snapshot",
                diagnostics={"phase": "apply_response", "http_status": 200,
                             "exception_type": type(exc).__name__},
            ) from exc
        trace_event("apply_response_completed", force_full=force_full,
                    updated_count=sum(result.get("updated", {}).values()),
                    deleted_count=sum(result.get("deleted", {}).values()))
        return result

    async def _post_diff(
        self,
        request_body: dict[str, Any],
        timeout: float,
        attempts: int,
    ) -> dict[str, Any]:
        response: httpx.Response | None = None
        request_bytes = len(httpx.Request("POST", ZENMONEY_API_URL, json=request_body).content)
        counts = {entity: len(items) for entity, items in request_body.items()
                  if entity in DIFF_FIELDS and isinstance(items, list)}

        async with httpx.AsyncClient() as client:
            for attempt in range(attempts):
                started = time.monotonic()
                request_info = {"http_request_id": str(uuid.uuid4()), "attempt": attempt + 1,
                                "max_attempts": attempts, "timeout_seconds": timeout,
                                "request_bytes": request_bytes, "entity_counts": counts}
                trace_event("http_request_started", **request_info)
                try:
                    response = await client.post(
                        ZENMONEY_API_URL,
                        json=request_body,
                        headers={
                            "Authorization": f"Bearer {self.token}",
                            "Content-Type": "application/json",
                        },
                        timeout=timeout,
                    )
                except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
                    details = {**request_info, "phase": "transport", **exception_details(exc),
                               "http_duration_ms": int((time.monotonic() - started) * 1000)}
                    trace_event("http_request_failed", **details)
                    if attempt + 1 == attempts:
                        raise SyncError(
                            f"HTTP error during sync after {attempts} attempts",
                            diagnostics=details,
                        ) from exc
                    continue
                except httpx.HTTPError as exc:
                    details = {**request_info, "phase": "transport", **exception_details(exc),
                               "http_duration_ms": int((time.monotonic() - started) * 1000)}
                    trace_event("http_request_failed", **details)
                    raise SyncError(
                        "HTTP error during sync",
                        diagnostics=details,
                    ) from exc
                response_info = {**request_info, "http_status": response.status_code,
                                 "http_duration_ms": int((time.monotonic() - started) * 1000)}
                content = getattr(response, "content", None)
                if isinstance(content, bytes):
                    response_info["response_bytes"] = len(content)
                trace_event("http_response_received", **response_info)
                break

        assert response is not None
        diagnostics: dict[str, Any] = response_info
        try:
            payload = response.json()
        except ValueError as exc:
            phase = "decode_response" if response.status_code == 200 else "http_response"
            diagnostics.update(phase=phase, exception_type=type(exc).__name__)
            trace_event("http_response_failed", **diagnostics)
            raise SyncError(
                f"ZenMoney API returned a non-JSON response (status {response.status_code})",
                diagnostics=diagnostics,
            ) from exc
        api_error = payload.get("error") if isinstance(payload, dict) else None
        if response.status_code != 200 or api_error is not None:
            if api_error is not None:
                diagnostics.update(_api_error_details(api_error, request_body))
            diagnostics["phase"] = "http_response"
            trace_event("http_response_failed", **diagnostics)
            raise SyncError(
                f"ZenMoney API returned an error (status {response.status_code})",
                diagnostics=diagnostics,
            )
        return payload


SyncEngine = HardenedSyncEngine
