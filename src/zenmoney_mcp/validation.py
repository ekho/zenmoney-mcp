"""Strict validation helpers for MCP tool inputs."""

from __future__ import annotations

import re
from datetime import date
from numbers import Real
from typing import Any


class ValidationError(ValueError):
    """Raised when an MCP tool argument is invalid."""


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_NAMED_PERIODS = {"this_month", "last_month", "last_30_days"}
_CURRENCY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,11}$")


def validate_currency_code(value: Any, field_name: str) -> str:
    """Validate a non-empty currency/instrument short code and normalize it."""
    if not isinstance(value, str) or not _CURRENCY_RE.fullmatch(value):
        raise ValidationError(f"{field_name} must be a valid currency code")
    return value.upper()


def validate_currency_list(
    value: Any,
    field_name: str = "currencies",
    *,
    maximum: int = 20,
) -> list[str]:
    """Validate a bounded currency-code list, preserving first-seen order."""
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise ValidationError(
            f"{field_name} must be a non-empty list with at most {maximum} currency codes"
        )
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        code = validate_currency_code(item, field_name)
        if code not in seen:
            seen.add(code)
            result.append(code)
    return result


def parse_iso_date(value: Any, field_name: str) -> date:
    """Parse an exact ``YYYY-MM-DD`` date or raise ``ValidationError``."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValidationError(f"{field_name} must be a valid date in YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(
            f"{field_name} must be a valid date in YYYY-MM-DD format"
        ) from exc


def validate_date_range(
    start_date: str | None,
    end_date: str | None,
) -> tuple[str | None, str | None]:
    """Validate optional inclusive date bounds."""
    parsed_start = parse_iso_date(start_date, "start_date") if start_date is not None else None
    parsed_end = parse_iso_date(end_date, "end_date") if end_date is not None else None
    if parsed_start is not None and parsed_end is not None and parsed_start > parsed_end:
        raise ValidationError("start_date must be on or before end_date")
    return start_date, end_date


def validate_period(period: Any, *, allow_none: bool = False) -> str | None:
    """Validate a named period or strict ``YYYY-MM`` calendar month."""
    if period is None and allow_none:
        return None
    if not isinstance(period, str):
        raise ValidationError(
            "period must be this_month, last_month, last_30_days, or YYYY-MM"
        )
    if period in _NAMED_PERIODS:
        return period
    match = _MONTH_RE.fullmatch(period)
    if not match:
        raise ValidationError(
            "period must be this_month, last_month, last_30_days, or YYYY-MM"
        )
    year, month = int(match.group(1)), int(match.group(2))
    try:
        date(year, month, 1)
    except ValueError as exc:
        raise ValidationError(
            "period must be this_month, last_month, last_30_days, or YYYY-MM"
        ) from exc
    return period


def bounded_int(
    value: Any,
    field_name: str,
    *,
    default: int | None = None,
    minimum: int,
    maximum: int,
) -> int:
    """Return an integer constrained to an inclusive range."""
    if value is None:
        if default is None:
            raise ValidationError(f"{field_name} is required")
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field_name} must be an integer")
    if value < minimum or value > maximum:
        raise ValidationError(
            f"{field_name} must be between {minimum} and {maximum}"
        )
    return value


def non_negative_number(
    value: Any,
    field_name: str,
    *,
    allow_none: bool = False,
) -> float | None:
    """Validate a finite non-negative numeric argument."""
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValidationError(f"{field_name} must be a non-negative number")
    numeric = float(value)
    if numeric < 0 or numeric != numeric or numeric in (float("inf"), float("-inf")):
        raise ValidationError(f"{field_name} must be a non-negative number")
    return numeric


def validate_amount_range(
    min_amount: Any,
    max_amount: Any,
) -> tuple[float | None, float | None]:
    """Validate optional non-negative amount bounds."""
    lower = non_negative_number(min_amount, "min_amount", allow_none=True)
    upper = non_negative_number(max_amount, "max_amount", allow_none=True)
    if lower is not None and upper is not None and lower > upper:
        raise ValidationError("min_amount must be less than or equal to max_amount")
    return lower, upper



# Compatibility name used by the hardened analytical layer.
InputValidationError = ValidationError


def resolve_date_range(
    period: str | None = "this_month",
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[str, str]:
    """Resolve a strict period or explicit inclusive date range."""
    from datetime import timedelta

    if end_date is not None and start_date is None:
        raise ValidationError("end_date requires start_date")
    validate_date_range(start_date, end_date)
    today = date.today()

    if start_date is not None:
        end = end_date or today.isoformat()
        return start_date, end

    checked_period = validate_period(period)
    assert checked_period is not None
    if checked_period == "this_month":
        start = today.replace(day=1)
        if today.month == 12:
            end = date(today.year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(today.year, today.month + 1, 1) - timedelta(days=1)
    elif checked_period == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif checked_period == "last_30_days":
        end = today
        start = today - timedelta(days=29)
    else:
        year, month = map(int, checked_period.split("-"))
        start = date(year, month, 1)
        if month == 12:
            end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(year, month + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()
