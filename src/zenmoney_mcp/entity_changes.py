"""Strict normalization and validation for ZenMoney user-entity changes."""

from __future__ import annotations

import math
import time
import uuid
from datetime import date
from typing import Any

from .hardened_database import HardenedDatabase, USER_ENTITY_TABLES, entity_key

ENTITY_TYPES = tuple(USER_ENTITY_TABLES)
UUID_ENTITY_TYPES = frozenset(ENTITY_TYPES) - {"budget"}
DIFF_FIELDS = {entity: entity for entity in ENTITY_TYPES}
TOTAL_BUDGET_TAG = "00000000-0000-0000-0000-000000000000"

EDITABLE = {
    "account": {
        "title", "type", "instrument", "company", "role", "syncID",
        "creditLimit", "inBalance", "savings", "enableCorrection", "enableSMS",
        "capitalization", "percent", "startDate", "endDateOffset",
        "endDateOffsetInterval", "payoffStep", "payoffInterval",
    },
    "tag": {
        "title", "parent", "icon", "picture", "color", "showIncome",
        "showOutcome", "budgetIncome", "budgetOutcome", "required",
    },
    "merchant": {"title"},
    "reminder": {
        "incomeInstrument", "incomeAccount", "income", "outcomeInstrument",
        "outcomeAccount", "outcome", "tag", "merchant", "payee", "comment",
        "interval", "step", "points", "startDate", "endDate", "notify",
    },
    "reminderMarker": {
        "incomeInstrument", "incomeAccount", "income", "outcomeInstrument",
        "outcomeAccount", "outcome", "tag", "merchant", "payee", "comment",
        "date", "reminder", "state", "notify",
    },
    "transaction": {
        "date", "income", "outcome", "incomeAccount", "outcomeAccount",
        "incomeInstrument", "outcomeInstrument", "tag", "merchant", "payee",
        "comment", "opIncome", "opOutcome", "opIncomeInstrument",
        "opOutcomeInstrument", "latitude", "longitude",
    },
    "budget": {"income", "incomeLock", "outcome", "outcomeLock"},
}

SAFE_DELETE = {
    "account": {"archive": True},
    "transaction": {"deleted": True},
    "reminderMarker": {"state": "deleted"},
    "budget": {
        "income": 0,
        "incomeLock": False,
        "outcome": 0,
        "outcomeLock": False,
    },
}

_ACCOUNT_DEFAULTS = {
    "private": False,
    "balanceCorrectionType": "request",
    "company": None,
    "role": None,
    "syncID": None,
    "startBalance": 0,
    "creditLimit": 0,
    "inBalance": True,
    "savings": False,
    "enableCorrection": True,
    "enableSMS": False,
    "archive": False,
    "capitalization": None,
    "percent": None,
    "startDate": None,
    "endDateOffset": None,
    "endDateOffsetInterval": None,
    "payoffStep": None,
    "payoffInterval": None,
}
_TAG_DEFAULTS = {
    "parent": None,
    "icon": None,
    "picture": None,
    "color": None,
    "showIncome": False,
    "showOutcome": False,
    "budgetIncome": False,
    "budgetOutcome": False,
    "required": None,
}
_MONEY_DEFAULTS = {
    "tag": None,
    "merchant": None,
    "payee": None,
    "comment": None,
}
_TRANSACTION_DEFAULTS = {
    **_MONEY_DEFAULTS,
    "deleted": False,
    "hold": None,
    "originalPayee": None,
    "mcc": None,
    "reminderMarker": None,
    "opIncome": None,
    "opOutcome": None,
    "opIncomeInstrument": None,
    "opOutcomeInstrument": None,
    "latitude": None,
    "longitude": None,
    "incomeBankID": None,
    "outcomeBankID": None,
}
_REMINDER_DEFAULTS = {
    **_MONEY_DEFAULTS,
    "interval": None,
    "step": 0,
    "points": [0],
    "endDate": None,
    "notify": False,
}
_MARKER_DEFAULTS = {**_MONEY_DEFAULTS, "notify": False}


class MutationValidationError(ValueError):
    """Raised when an operation cannot be made safe and deterministic."""


class MutationStateError(ValueError):
    """Raised when the local snapshot cannot support mutation preparation."""


def _timestamp(value: int | None) -> int:
    return int(time.time()) if value is None else value


def _number(value: Any, field: str, *, non_negative: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MutationValidationError(f"{field} must be a number")
    if not math.isfinite(value):
        raise MutationValidationError(f"{field} must be finite")
    if non_negative and value < 0:
        raise MutationValidationError(f"{field} must be non-negative")
    return float(value)


def _integer(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MutationValidationError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise MutationValidationError(f"{field} must be at least {minimum}")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise MutationValidationError(f"{field} must be a boolean")
    return value


def _string(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        suffix = " or null" if nullable else ""
        raise MutationValidationError(f"{field} must be a non-empty string{suffix}")
    return value


def _date(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise MutationValidationError(f"{field} must be a real ISO date") from exc
    if parsed.isoformat() != value:
        raise MutationValidationError(f"{field} must be a real ISO date")
    return value


def _enum(value: Any, field: str, choices: set[str], *, nullable: bool = False):
    if value is None and nullable:
        return None
    if value not in choices:
        raise MutationValidationError(f"{field} has an invalid value")
    return value


def _exists(db: HardenedDatabase, table: str, object_id: Any) -> bool:
    return db.connect().execute(
        f"SELECT 1 FROM {table} WHERE id=?", (object_id,)
    ).fetchone() is not None


def _owner_for_create(
    db: HardenedDatabase, requested: Any
) -> int:
    users = [int(row["id"]) for row in db.connect().execute("SELECT id FROM users")]
    if requested is None:
        if len(users) != 1:
            raise MutationValidationError(
                "owner_user_id is required when the snapshot has multiple users"
            )
        return users[0]
    owner = _integer(requested, "owner_user_id")
    if owner not in users:
        raise MutationValidationError("owner_user_id does not exist")
    return owner


def _row_owner(db: HardenedDatabase, entity_type: str, object_id: Any) -> int | None:
    table = USER_ENTITY_TABLES[entity_type]
    row = db.connect().execute(
        f"SELECT user FROM {table} WHERE id=?", (object_id,)
    ).fetchone()
    return None if row is None else row["user"]


def _resolve_reference(
    db: HardenedDatabase,
    value: Any,
    expected_type: str,
    owner: int,
    refs: dict[str, dict[str, Any]],
    field: str,
    *,
    nullable: bool = False,
) -> Any:
    if value is None and nullable:
        return None
    if isinstance(value, dict):
        if set(value) != {"ref"} or not isinstance(value["ref"], str):
            raise MutationValidationError(f"{field} ref is invalid")
        target = refs.get(value["ref"])
        if target is None:
            raise MutationValidationError(f"{field} ref is unresolved")
        if target["entity_type"] != expected_type:
            raise MutationValidationError(f"{field} ref has incompatible type")
        if target["owner"] != owner:
            raise MutationValidationError(f"{field} ref has incompatible owner")
        if field == "parent" and target.get("parent") is not None:
            raise MutationValidationError("tag nesting cannot exceed one level")
        return target["id"]

    if expected_type in {"instrument", "company", "user"}:
        table = {"instrument": "instruments", "company": "companies", "user": "users"}[
            expected_type
        ]
        object_id = _integer(value, field)
        if not _exists(db, table, object_id):
            raise MutationValidationError(f"{field} does not exist")
        return object_id

    object_id = _string(value, field)
    reference_owner = _row_owner(db, expected_type, object_id)
    if reference_owner is None:
        raise MutationValidationError(f"{field} does not exist")
    if int(reference_owner) != owner:
        raise MutationValidationError(f"{field} has incompatible owner")
    return object_id


def _resolve_fields(
    db: HardenedDatabase,
    entity_type: str,
    value: dict[str, Any],
    owner: int,
    refs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    result = dict(value)
    scalar_refs = {
        "account": {"instrument": "instrument", "company": "company", "role": "user"},
        "tag": {"parent": "tag"},
        "reminder": {
            "incomeInstrument": "instrument", "outcomeInstrument": "instrument",
            "incomeAccount": "account", "outcomeAccount": "account",
            "merchant": "merchant",
        },
        "reminderMarker": {
            "incomeInstrument": "instrument", "outcomeInstrument": "instrument",
            "incomeAccount": "account", "outcomeAccount": "account",
            "merchant": "merchant", "reminder": "reminder",
        },
        "transaction": {
            "incomeInstrument": "instrument", "outcomeInstrument": "instrument",
            "incomeAccount": "account", "outcomeAccount": "account",
            "merchant": "merchant", "opIncomeInstrument": "instrument",
            "opOutcomeInstrument": "instrument",
        },
        "budget": {"tag": "tag"},
    }.get(entity_type, {})
    nullable = {
        "company", "role", "parent", "merchant", "opIncomeInstrument",
        "opOutcomeInstrument", "tag",
    }
    for field, expected_type in scalar_refs.items():
        if field in result:
            if entity_type == "budget" and field == "tag" and result[field] == TOTAL_BUDGET_TAG:
                continue
            result[field] = _resolve_reference(
                db,
                result[field],
                expected_type,
                owner,
                refs,
                field,
                nullable=field in nullable,
            )
    if "tag" in result and entity_type in {"reminder", "reminderMarker", "transaction"}:
        tags = result["tag"]
        if tags is None:
            return result
        if not isinstance(tags, list):
            raise MutationValidationError("tag must be an array or null")
        result["tag"] = [
            _resolve_reference(db, tag, "tag", owner, refs, "tag")
            for tag in tags
        ]
        if len(result["tag"]) != len(set(result["tag"])):
            raise MutationValidationError("tag must contain unique ids")
    return result


def _validate_money_object(value: dict[str, Any], *, transaction: bool) -> None:
    income = _number(value.get("income"), "income")
    outcome = _number(value.get("outcome"), "outcome")
    if transaction and not value.get("deleted") and income <= 0 and outcome <= 0:
        raise MutationValidationError("income or outcome must be positive")
    for field in ("incomeAccount", "outcomeAccount"):
        _string(value.get(field), field)
    for field in ("incomeInstrument", "outcomeInstrument"):
        _integer(value.get(field), field)
    tags = value.get("tag")
    if tags is not None:
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise MutationValidationError("tag must be an array or null")
        if len(tags) != len(set(tags)):
            raise MutationValidationError("tag must contain unique ids")
    merchant = value.get("merchant")
    if merchant is not None:
        _string(merchant, "merchant")
    for field in ("payee", "comment"):
        if value.get(field) is not None:
            _string(value[field], field)


def _validate_entity(
    db: HardenedDatabase, entity_type: str, value: dict[str, Any], *, creating: bool
) -> None:
    owner = _integer(value.get("user"), "user")
    if not _exists(db, "users", owner):
        raise MutationValidationError("user does not exist")

    if entity_type == "account":
        _string(value.get("title"), "title")
        account_type = _enum(
            value.get("type"),
            "type",
            {"cash", "ccard", "checking", "loan", "deposit", "emoney", "debt"},
        )
        if creating and account_type == "debt":
            raise MutationValidationError("debt account cannot be created")
        if value.get("instrument") is not None:
            _integer(value["instrument"], "instrument")
        elif creating:
            raise MutationValidationError("instrument must be an integer")
        if value.get("company") is not None:
            _integer(value["company"], "company")
        if value.get("role") is not None:
            _integer(value["role"], "role")
        for field in ("balance", "startBalance"):
            if value.get(field) is not None:
                _number(value[field], field, non_negative=False)
            elif creating:
                raise MutationValidationError(f"{field} must be a number")
        if value.get("creditLimit") is not None:
            _number(value["creditLimit"], "creditLimit")
        for field in ("inBalance", "enableCorrection", "enableSMS", "archive"):
            _boolean(value.get(field), field)
        if value.get("savings") is not None:
            _boolean(value["savings"], "savings")
        if value.get("syncID") is not None:
            sync_ids = value["syncID"]
            if not isinstance(sync_ids, list) or any(not isinstance(item, str) for item in sync_ids):
                raise MutationValidationError("syncID must be an array or null")
        if value.get("percent") is not None:
            percent = _number(value["percent"], "percent")
            if percent >= 100:
                raise MutationValidationError("percent must be below 100")
        if value.get("startDate") is not None:
            _date(value["startDate"], "startDate")
        if value.get("endDateOffset") is not None:
            _integer(value["endDateOffset"], "endDateOffset", minimum=0)
        if value.get("endDateOffsetInterval") is not None:
            _enum(value["endDateOffsetInterval"], "endDateOffsetInterval", {"day", "week", "month", "year"})
        if value.get("payoffStep") is not None:
            _integer(value["payoffStep"], "payoffStep", minimum=0)
        if value.get("payoffInterval") is not None:
            _enum(value["payoffInterval"], "payoffInterval", {"month", "year"})
        if value.get("capitalization") is not None:
            _boolean(value["capitalization"], "capitalization")
        return

    if entity_type == "tag":
        _string(value.get("title"), "title")
        for field in ("icon", "picture"):
            if value.get(field) is not None:
                _string(value[field], field)
        if value.get("color") is not None:
            color = _integer(value["color"], "color", minimum=0)
            if color > 0xFFFFFFFF:
                raise MutationValidationError("color is out of range")
        for field in ("showIncome", "showOutcome", "budgetIncome", "budgetOutcome"):
            _boolean(value.get(field), field)
        if value.get("required") is not None:
            _boolean(value["required"], "required")
        parent = value.get("parent")
        if parent is not None:
            _string(parent, "parent")
            parent_raw = db.get_entity_raw("tag", entity_key("tag", {"id": parent}))
            if parent_raw is not None and parent_raw.get("parent") is not None:
                raise MutationValidationError("tag nesting cannot exceed one level")
        return

    if entity_type == "merchant":
        _string(value.get("title"), "title")
        return

    if entity_type in {"reminder", "reminderMarker", "transaction"}:
        _validate_money_object(value, transaction=entity_type == "transaction")
        if entity_type == "transaction":
            _date(value.get("date"), "date")
            _boolean(value.get("deleted"), "deleted")
            for amount_field, instrument_field in (
                ("opIncome", "opIncomeInstrument"),
                ("opOutcome", "opOutcomeInstrument"),
            ):
                amount = value.get(amount_field)
                instrument = value.get(instrument_field)
                if amount is not None:
                    _number(amount, amount_field)
                if instrument is not None:
                    _integer(instrument, instrument_field)
                if (
                    amount is None and instrument is not None
                    or amount not in (None, 0) and instrument is None
                ):
                    raise MutationValidationError(
                        f"{amount_field} and {instrument_field} must be paired"
                    )
                if amount is not None:
                    _number(amount, amount_field)
            for field, minimum, maximum in (
                ("latitude", -90, 90), ("longitude", -180, 180)
            ):
                coordinate = value.get(field)
                if coordinate is not None:
                    coordinate = _number(coordinate, field, non_negative=False)
                    if not minimum <= coordinate <= maximum:
                        raise MutationValidationError(f"{field} is out of range")
            return
        if entity_type == "reminder":
            _date(value.get("startDate"), "startDate")
            _date(value.get("endDate"), "endDate", nullable=True)
            interval = _enum(
                value.get("interval"),
                "interval",
                {"day", "week", "month", "year"},
                nullable=True,
            )
            step = value.get("step")
            points = value.get("points")
            if interval is None:
                if (step, points) not in ((None, None), (0, [0])):
                    raise MutationValidationError(
                        "step and points must use the non-recurring canonical values"
                    )
            else:
                step = _integer(step, "step", minimum=1)
                if points is not None:
                    if not isinstance(points, list) or any(
                        isinstance(point, bool) or not isinstance(point, int)
                        or point < 0 or point >= step for point in points
                    ):
                        raise MutationValidationError("points are invalid")
            _boolean(value.get("notify"), "notify")
            return
        _date(value.get("date"), "date")
        _enum(value.get("state"), "state", {"planned", "processed", "deleted"})
        _string(value.get("reminder"), "reminder")
        _boolean(value.get("notify"), "notify")
        return

    _date(value.get("date"), "date")
    if date.fromisoformat(value["date"]).day != 1:
        raise MutationValidationError("budget date must be the first day of a month")
    for field in ("income", "outcome"):
        _number(value.get(field), field)
    for field in ("incomeLock", "outcomeLock"):
        _boolean(value.get(field), field)
    if value.get("tag") is not None:
        _string(value["tag"], "tag")


def _create_defaults(entity_type: str) -> dict[str, Any]:
    return {
        "account": _ACCOUNT_DEFAULTS,
        "tag": _TAG_DEFAULTS,
        "merchant": {},
        "reminder": _REMINDER_DEFAULTS,
        "reminderMarker": _MARKER_DEFAULTS,
        "transaction": _TRANSACTION_DEFAULTS,
        "budget": {},
    }[entity_type]


def _budget_key(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"owner_user_id", "tag", "date"}:
        raise MutationValidationError(
            "budget key requires owner_user_id, tag, and date"
        )
    owner = _integer(value["owner_user_id"], "owner_user_id")
    tag = value["tag"]
    if tag is not None:
        _string(tag, "tag")
    return {"user": owner, "tag": tag, "date": _date(value["date"], "date")}


def _raw_for_operation(
    db: HardenedDatabase, entity_type: str, operation: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    if entity_type == "budget":
        identity = _budget_key(operation.get("key"))
    else:
        identity = {"id": _string(operation.get("id"), "id")}
    key = entity_key(entity_type, identity)
    raw = db.get_entity_raw(entity_type, key)
    if raw is None:
        raise MutationValidationError(f"{entity_type} does not exist in the full snapshot")
    return key, raw


def normalize_operations(
    db: HardenedDatabase,
    operations: Any,
    entity_type: str | None = None,
    now: int | None = None,
) -> list[dict[str, Any]]:
    """Resolve and validate a frozen ordered change set."""
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise MutationValidationError("operations must contain 1 to 100 items")
    if entity_type is not None and entity_type not in ENTITY_TYPES:
        raise MutationValidationError("unsupported entity type")

    timestamp = _timestamp(now)
    refs: dict[str, dict[str, Any]] = {}
    identities: set[tuple[str, str]] = set()
    normalized: list[dict[str, Any]] = []

    for operation in operations:
        if not isinstance(operation, dict):
            raise MutationValidationError("each operation must be an object")
        if entity_type is None:
            current_type = operation.get("entity")
            if current_type not in ENTITY_TYPES:
                raise MutationValidationError("entity is unsupported")
            body = {key: value for key, value in operation.items() if key != "entity"}
        else:
            if "entity" in operation:
                raise MutationValidationError("entity is not accepted by entity-specific tools")
            current_type = entity_type
            body = dict(operation)

        action = body.get("operation")
        if action not in {"create", "update", "delete"}:
            raise MutationValidationError("operation must be create, update, or delete")

        if action == "create":
            allowed = {"operation", "value", "ref", "owner_user_id"}
            if set(body) - allowed or "value" not in body:
                raise MutationValidationError("create operation fields are invalid")
            value = body["value"]
            if not isinstance(value, dict):
                raise MutationValidationError("value must be an object")
            allowed_value = set(EDITABLE[current_type])
            if current_type == "account":
                allowed_value.add("startBalance")
            elif current_type == "budget":
                allowed_value.update({"date", "tag"})
            forbidden = set(value) - allowed_value
            if forbidden:
                raise MutationValidationError(
                    f"field {sorted(forbidden)[0]} is not editable"
                )
            owner = _owner_for_create(db, body.get("owner_user_id"))
            resolved = {**_create_defaults(current_type), **value}
            resolved = _resolve_fields(db, current_type, resolved, owner, refs)
            resolved["user"] = owner
            resolved["changed"] = timestamp
            entity_id = None
            if current_type in UUID_ENTITY_TYPES:
                entity_id = str(uuid.uuid4())
                resolved["id"] = entity_id
            if current_type == "account":
                resolved["balance"] = resolved["startBalance"]
            if current_type == "transaction":
                resolved["created"] = timestamp
            _validate_entity(db, current_type, resolved, creating=True)
            key = entity_key(current_type, resolved)
            if db.get_entity_raw(current_type, key) is not None:
                raise MutationValidationError("create identity already exists")
            item = {
                "entity_type": current_type,
                "entity_key": key,
                "entity_id": entity_id,
                "operation": action,
                "expected_changed": None,
                "before": None,
                "after": resolved,
                "resolved": resolved,
            }
            ref = body.get("ref")
            if ref is not None:
                if current_type == "budget":
                    raise MutationValidationError("budget create does not support ref")
                if not isinstance(ref, str) or not ref:
                    raise MutationValidationError("ref must be a non-empty string")
                if ref in refs:
                    raise MutationValidationError("duplicate ref")
                refs[ref] = {
                    "entity_type": current_type,
                    "id": entity_id,
                    "owner": owner,
                    "parent": resolved.get("parent") if current_type == "tag" else None,
                }
        else:
            if current_type == "budget":
                allowed = {"operation", "key"}
            else:
                allowed = {"operation", "id"}
            if action == "update":
                allowed.add("set")
            if set(body) != allowed:
                raise MutationValidationError(f"{action} operation fields are invalid")
            if action == "delete" and current_type not in SAFE_DELETE:
                raise MutationValidationError(
                    f"safe delete is not supported for {current_type}"
                )
            key, raw = _raw_for_operation(db, current_type, body)
            owner = _integer(raw.get("user"), "user")
            changed = raw.get("changed")
            _integer(changed, "changed")
            if action == "update":
                patch = body["set"]
                if not isinstance(patch, dict) or not patch:
                    raise MutationValidationError("set must be a non-empty object")
                forbidden = set(patch) - EDITABLE[current_type]
                if forbidden:
                    raise MutationValidationError(
                        f"field {sorted(forbidden)[0]} is not editable"
                    )
                patch = _resolve_fields(db, current_type, patch, owner, refs)
                if current_type == "reminderMarker" and raw.get("state") == "deleted":
                    raise MutationValidationError("deleted reminder marker cannot be restored")
                if current_type == "reminderMarker" and patch.get("state") == "deleted":
                    raise MutationValidationError("use delete operation for deleted state")
            else:
                patch = dict(SAFE_DELETE[current_type])
            result = {**raw, **patch}
            _validate_entity(db, current_type, result, creating=False)
            before = {field: raw.get(field) for field in patch}
            after = {field: result.get(field) for field in patch}
            if before == after:
                raise MutationValidationError("operation does not change the entity")
            item = {
                "entity_type": current_type,
                "entity_key": key,
                "entity_id": raw.get("id"),
                "operation": action,
                "expected_changed": changed,
                "before": before,
                "after": after,
                "resolved": patch,
            }

        identity = (current_type, item["entity_key"])
        if identity in identities:
            raise MutationValidationError("duplicate entity identity")
        identities.add(identity)
        normalized.append(item)

    return normalized


def rebuild_after(
    db: HardenedDatabase,
    item: dict[str, Any],
    raw: dict[str, Any] | None,
) -> dict[str, Any]:
    """Rebuild one complete outgoing object from a frozen normalized item."""
    if item["operation"] == "create":
        result = dict(item["after"])
    else:
        if raw is None:
            raise MutationValidationError("existing entity raw object is missing")
        result = {**raw, **item["after"]}
    _validate_entity(db, item["entity_type"], result, creating=item["operation"] == "create")
    return result


def verify_after(item: dict[str, Any], raw: dict[str, Any] | None) -> bool:
    """Compare stable expected fields after a verification sync."""
    if raw is None:
        return (
            item["entity_type"] == "reminderMarker"
            and item["operation"] == "delete"
        )
    expected = item["after"]
    ignored = {"changed"}
    if item["entity_type"] == "account" and item["operation"] == "create":
        ignored.add("balance")
    return all(
        key in ignored or raw.get(key) == value
        for key, value in expected.items()
    )
