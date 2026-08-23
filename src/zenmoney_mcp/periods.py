"""Budget-period boundaries for planning analytics."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .validation import InputValidationError


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    label: str
    complete: bool


def _month_start(year: int, month: int, start_day: int) -> date:
    return date(year, month, min(start_day, calendar.monthrange(year, month)[1]))


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _period_for_month(year: int, month: int, start_day: int, complete: bool) -> Period:
    start = _month_start(year, month, start_day)
    next_year, next_month = _next_month(year, month)
    end = _month_start(next_year, next_month, start_day) - timedelta(days=1)
    return Period(start, end, f"{year:04d}-{month:02d}", complete)


def _current_period_for_day(start_day: int, as_of: date) -> Period:
    year, month = as_of.year, as_of.month
    if as_of < _month_start(year, month, start_day):
        year, month = _previous_month(year, month)
    return _period_for_month(year, month, start_day, False)


def current_period(db: Any, as_of: date | None = None) -> Period:
    return _current_period_for_day(
        db.get_user_month_start_day(), as_of or date.today()
    )


def completed_periods(db: Any, count: int, as_of: date | None = None) -> list[Period]:
    """Return completed budget periods oldest first."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("count must be a positive integer")
    current = current_period(db, as_of)
    year, month = _previous_month(current.start.year, current.start.month)
    start_day = db.get_user_month_start_day()
    periods = []
    for _ in range(count):
        periods.append(_period_for_month(year, month, start_day, True))
        year, month = _previous_month(year, month)
    return list(reversed(periods))


_TRAILING_MONTHS = {
    "trailing_3_complete_months": 3,
    "trailing_6_complete_months": 6,
    "trailing_12_complete_months": 12,
}


def resolve_period(
    db: Any,
    preset: str = "current_period",
    start_date: str | None = None,
    end_date: str | None = None,
    as_of: date | None = None,
) -> Period:
    """Resolve a cash-flow preset or a strict custom inclusive range."""
    today = as_of or date.today()
    if start_date is not None or end_date is not None:
        if not start_date or not end_date:
            raise InputValidationError("start_date and end_date must be provided together")
        try:
            start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        except ValueError as exc:
            raise InputValidationError("dates must use YYYY-MM-DD format") from exc
        if end < start:
            raise InputValidationError("end_date must be on or after start_date")
        return Period(start, end, "custom", end < today)
    if preset == "current_period":
        period = current_period(db, today)
        return Period(period.start, min(period.end, today), preset, False)
    if preset == "last_30_days":
        return Period(today - timedelta(days=29), today, preset, False)
    if preset == "last_complete_month":
        source = completed_periods(db, 1, today)[0]
        return Period(source.start, source.end, preset, True)
    months = _TRAILING_MONTHS.get(preset)
    if months is not None:
        source = completed_periods(db, months, today)
        return Period(source[0].start, source[-1].end, preset, True)
    raise InputValidationError(f"unsupported period preset: {preset}")


def complete_months_with_activity(db: Any, as_of: date | None = None) -> int:
    """Count completed activity periods with one bounded aggregate result."""
    today = as_of or date.today()
    current = current_period(db, today)
    start_day = db.get_user_month_start_day()
    row = db.connect().execute(
        """SELECT COUNT(DISTINCT CASE
                 WHEN CAST(strftime('%d',t.date) AS INTEGER) >= MIN(
                      ?,
                      CAST(strftime('%d',date(t.date,'start of month','+1 month','-1 day')) AS INTEGER)
                 )
                 THEN strftime('%Y-%m',t.date)
                 ELSE strftime('%Y-%m',date(t.date,'start of month','-1 month'))
               END) AS count
           FROM transactions t
           LEFT JOIN accounts ia ON ia.id=t.income_account
           LEFT JOIN accounts oa ON oa.id=t.outcome_account
           WHERE COALESCE(t.deleted,0)=0 AND COALESCE(t.hold,0)=0
             AND ((t.income>0 AND t.outcome=0 AND COALESCE(ia.archive,0)=0
                   AND (ia.in_balance=1 OR ia.in_balance IS NULL))
               OR (t.outcome>0 AND t.income=0 AND COALESCE(oa.archive,0)=0
                   AND (oa.in_balance=1 OR oa.in_balance IS NULL)))
             AND t.date < ?""",
        (start_day, current.start.isoformat()),
    ).fetchone()
    return int(row["count"] or 0)


def comparison_periods(
    db: Any, preset: str, as_of: date | None = None
) -> tuple[Period, Period]:
    """Resolve an earlier period A and later period B for comparison."""
    today = as_of or date.today()
    if preset == "last_month_vs_previous":
        source = completed_periods(db, 2, today)
        return source[0], source[1]
    if preset == "last_quarter_vs_previous":
        source = completed_periods(db, 6, today)
        return (
            Period(source[0].start, source[2].end, "previous_quarter", True),
            Period(source[3].start, source[5].end, "last_quarter", True),
        )
    if preset == "last_complete_month_vs_year_ago":
        latest = completed_periods(db, 1, today)[0]
        previous_year = _period_for_month(
            latest.start.year - 1,
            latest.start.month,
            db.get_user_month_start_day(),
            True,
        )
        return previous_year, latest
    raise InputValidationError(f"unsupported comparison preset: {preset}")
