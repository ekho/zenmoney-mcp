"""Validated, atomic ZenMoney synchronization."""

from __future__ import annotations

import time
from typing import Any

import httpx

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


class SyncError(Exception):
    """Raised when synchronization cannot be validated or completed."""


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
        started = time.time()
        request_body = {
            "currentClientTimestamp": int(time.time()),
            "serverTimestamp": 0 if force_full else self.db.get_server_timestamp(),
        }
        timeout = 300.0 if force_full else 60.0
        attempts = 2 if force_full else 1
        response: httpx.Response | None = None

        async with httpx.AsyncClient() as client:
            for attempt in range(attempts):
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
                    break
                except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
                    if attempt + 1 == attempts:
                        raise SyncError(
                            f"HTTP error during sync after {attempts} attempts: {exc}"
                        ) from exc
                except httpx.HTTPError as exc:
                    raise SyncError(f"HTTP error during sync: {exc}") from exc

        assert response is not None
        if response.status_code != 200:
            raise SyncError(f"ZenMoney API returned status {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SyncError(f"ZenMoney API returned invalid JSON: {exc}") from exc

        result = self.apply_diff_data(
            payload,
            force_full=force_full,
            last_sync_time=int(time.time()),
        )
        result["sync_duration_ms"] = int((time.time() - started) * 1000)
        return result


SyncEngine = HardenedSyncEngine
