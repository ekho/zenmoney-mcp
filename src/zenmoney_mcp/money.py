"""Strict currency primitives backed by synchronized ZenMoney rates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class FinancialDataError(ValueError):
    """Raised when the cache lacks reference data required for analytics."""


@dataclass(frozen=True)
class CurrencyContext:
    instrument_id: int
    code: str
    symbol: str
    rate: float


def user_currency(db: Any) -> CurrencyContext:
    instrument_id = db.get_user_currency()
    if instrument_id is None:
        raise FinancialDataError("primary user currency is missing from the cache")
    row = db.connect().execute(
        "SELECT short_title,symbol FROM instruments WHERE id=?", (instrument_id,)
    ).fetchone()
    if row is None:
        raise FinancialDataError(
            f"primary user currency instrument {instrument_id} is missing"
        )
    return CurrencyContext(
        int(instrument_id),
        row["short_title"] or str(instrument_id),
        row["symbol"] or "",
        db.require_instrument_rate(int(instrument_id)),
    )


def convert(
    db: Any,
    amount: float | int | None,
    instrument_id: int | None,
    target: CurrencyContext,
) -> float:
    numeric = float(amount or 0)
    if numeric == 0:
        return 0.0
    if instrument_id is None:
        raise FinancialDataError("currency instrument is missing for a non-zero amount")
    return numeric * db.require_instrument_rate(int(instrument_id)) / target.rate
