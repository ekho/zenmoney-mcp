"""Bounded MCP read resources for ZenMoney user entities."""

from __future__ import annotations

import base64
import json
from typing import Any

from .entity_changes import ENTITY_TYPES
from .hardened_database import HardenedDatabase, entity_key

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

PUBLIC_FIELDS = {
    "account": (
        "id", "changed", "role", "instrument", "company", "type", "title",
        "syncID", "balance", "startBalance", "creditLimit", "inBalance",
        "savings", "enableCorrection", "enableSMS", "archive",
        "capitalization", "percent", "startDate", "endDateOffset",
        "endDateOffsetInterval", "payoffStep", "payoffInterval",
    ),
    "tag": (
        "id", "changed", "title", "parent", "icon", "picture", "color",
        "showIncome", "showOutcome", "budgetIncome", "budgetOutcome", "required",
    ),
    "merchant": ("id", "changed", "title"),
    "reminder": (
        "id", "changed", "incomeInstrument", "incomeAccount", "income",
        "outcomeInstrument", "outcomeAccount", "outcome", "tag", "merchant",
        "payee", "comment", "interval", "step", "points", "startDate",
        "endDate", "notify",
    ),
    "reminderMarker": (
        "id", "changed", "incomeInstrument", "incomeAccount", "income",
        "outcomeInstrument", "outcomeAccount", "outcome", "tag", "merchant",
        "payee", "comment", "date", "reminder", "state", "notify",
    ),
    "transaction": (
        "id", "changed", "created", "deleted", "hold", "incomeInstrument",
        "incomeAccount", "income", "outcomeInstrument", "outcomeAccount",
        "outcome", "tag", "merchant", "payee", "originalPayee", "comment",
        "date", "mcc", "reminderMarker", "opIncome", "opIncomeInstrument",
        "opOutcome", "opOutcomeInstrument", "latitude", "longitude",
    ),
    "budget": (
        "changed", "tag", "date", "income", "incomeLock", "outcome",
        "outcomeLock",
    ),
}

ACTIVE_SQL = {
    "account": "COALESCE(json_extract(raw_json,'$.archive'),0)=0",
    "transaction": "COALESCE(json_extract(raw_json,'$.deleted'),0)=0",
    "reminderMarker": "COALESCE(json_extract(raw_json,'$.state'),'')!='deleted'",
    "budget": "(COALESCE(json_extract(raw_json,'$.incomeLock'),0)!=0 "
              "OR COALESCE(json_extract(raw_json,'$.outcomeLock'),0)!=0 "
              "OR COALESCE(json_extract(raw_json,'$.income'),0)!=0 "
              "OR COALESCE(json_extract(raw_json,'$.outcome'),0)!=0)",
}


class EntityResourceError(ValueError):
    """Fixed public resource error."""


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def encode_cursor(entity_type: str, entity_key_value: str) -> str:
    payload = _json([entity_type, entity_key_value]).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: Any, entity_type: str) -> str:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
        raise EntityResourceError("cursor_invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            cursor + padding, altchars=b"-_", validate=True
        )
        value = json.loads(decoded)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise EntityResourceError("cursor_invalid") from exc
    if (
        not isinstance(value, list)
        or len(value) != 2
        or value[0] != entity_type
        or not isinstance(value[1], str)
        or encode_cursor(value[0], value[1]) != cursor
    ):
        raise EntityResourceError("cursor_invalid")
    return value[1]


def _public(entity_type: str, raw_json: str) -> dict[str, Any]:
    try:
        raw = json.loads(raw_json)
    except ValueError as exc:
        raise EntityResourceError("entity_snapshot_invalid") from exc
    if not isinstance(raw, dict):
        raise EntityResourceError("entity_snapshot_invalid")
    result = {
        field: raw[field]
        for field in PUBLIC_FIELDS[entity_type]
        if field in raw
    }
    if "user" in raw:
        result["owner_user_id"] = raw["user"]
    return result


def _validate_entity_type(entity_type: Any) -> str:
    if entity_type not in ENTITY_TYPES:
        raise EntityResourceError("entity_type_invalid")
    return entity_type


def list_entity_resource(
    db: HardenedDatabase,
    entity_type: str,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    include_inactive: bool = False,
) -> dict[str, Any]:
    """Return one stable keyset page without exposing stored raw JSON."""
    entity_type = _validate_entity_type(entity_type)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise EntityResourceError("limit_invalid")
    if not isinstance(include_inactive, bool):
        raise EntityResourceError("include_inactive_invalid")
    last_key = decode_cursor(cursor, entity_type) if cursor is not None else None

    conditions = ["entity_type=?"]
    params: list[Any] = [entity_type]
    if last_key is not None:
        conditions.append("entity_key>?")
        params.append(last_key)
    if not include_inactive and entity_type in ACTIVE_SQL:
        conditions.append(ACTIVE_SQL[entity_type])
    params.append(limit + 1)
    rows = db.connect().execute(
        "SELECT entity_key,raw_json FROM entity_raw WHERE "
        + " AND ".join(conditions)
        + " ORDER BY entity_key LIMIT ?",
        params,
    ).fetchall()
    has_more = len(rows) > limit
    page = rows[:limit]
    return {
        "items": [_public(entity_type, row["raw_json"]) for row in page],
        "next_cursor": (
            encode_cursor(entity_type, page[-1]["entity_key"])
            if has_more and page
            else None
        ),
    }


def get_entity_resource(
    db: HardenedDatabase,
    entity_type: str,
    key_parts: Any,
) -> dict[str, Any]:
    """Return one exact user entity, including an inactive entity."""
    entity_type = _validate_entity_type(entity_type)
    if entity_type == "budget":
        if (
            not isinstance(key_parts, dict)
            or set(key_parts) != {"owner_user_id", "tag", "date"}
        ):
            raise EntityResourceError("entity_key_invalid")
        identity = {
            "user": key_parts["owner_user_id"],
            "tag": key_parts["tag"],
            "date": key_parts["date"],
        }
    else:
        if not isinstance(key_parts, str) or not key_parts:
            raise EntityResourceError("entity_key_invalid")
        identity = {"id": key_parts}
    key = entity_key(entity_type, identity)
    row = db.connect().execute(
        "SELECT raw_json FROM entity_raw WHERE entity_type=? AND entity_key=?",
        (entity_type, key),
    ).fetchone()
    if row is None:
        raise EntityResourceError("entity_not_found")
    return _public(entity_type, row["raw_json"])
