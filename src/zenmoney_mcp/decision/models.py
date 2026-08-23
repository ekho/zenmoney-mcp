"""Shared Decimal and calendar-month primitives for planning."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from numbers import Real
from typing import Any

from ..validation import InputValidationError

CENT = Decimal("0.01")
HUNDRED = Decimal("100")


def decimal_number(value: Any, field: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise InputValidationError(f"{field} must be a finite number")
    result = Decimal(str(value))
    if not result.is_finite() or (minimum is not None and result < minimum):
        raise InputValidationError(f"{field} must be at least {minimum}")
    return result


def money(value: Decimal | Real) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def number(value: Decimal | Real) -> float:
    return float(money(value))


def ceiling_ratio(numerator: Decimal, denominator: Decimal) -> int:
    return int((numerator / denominator).to_integral_value(rounding=ROUND_CEILING))


def month_end_after(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    return date(year, month, calendar.monthrange(year, month)[1])
