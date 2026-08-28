"""Financially explicit analytics for the hardened ZenMoney runtime."""

from __future__ import annotations

import base64
import json
import math
from datetime import date, timedelta
from typing import Any, Iterable

from .hardened_database import CurrencyRateError, HardenedDatabase
from .money import FinancialDataError
from .validation import (
    InputValidationError,
    bounded_int,
    non_negative_number,
    resolve_date_range,
    validate_amount_range,
    validate_currency_code,
    validate_currency_list,
    validate_period,
)

_TOTAL_BUDGET_TAG = "00000000-0000-0000-0000-000000000000"


def _user_currency(db: HardenedDatabase) -> tuple[int, str, str, float]:
    currency_id = db.get_user_currency()
    if currency_id is None:
        raise FinancialDataError("primary user currency is missing from the cache")
    row = db.connect().execute(
        "SELECT short_title, symbol, rate FROM instruments WHERE id = ?",
        (currency_id,),
    ).fetchone()
    if row is None:
        raise FinancialDataError(
            f"primary user currency instrument {currency_id} is missing"
        )
    rate = db.require_instrument_rate(currency_id)
    return (
        int(currency_id),
        row["short_title"] or str(currency_id),
        row["symbol"] or "",
        rate,
    )


def _instrument(db: HardenedDatabase, instrument_id: int | None) -> tuple[str, str, float]:
    if instrument_id is None:
        raise FinancialDataError("transaction/account currency instrument is missing")
    row = db.connect().execute(
        "SELECT short_title, symbol FROM instruments WHERE id = ?",
        (instrument_id,),
    ).fetchone()
    if row is None:
        raise FinancialDataError(f"currency instrument {instrument_id} is missing")
    return row["short_title"] or str(instrument_id), row["symbol"] or "", db.require_instrument_rate(instrument_id)


def _convert(
    db: HardenedDatabase,
    amount: float | int | None,
    instrument_id: int | None,
    user_currency_id: int,
    user_rate: float,
) -> float:
    numeric = float(amount or 0)
    if instrument_id is None:
        if numeric == 0:
            return 0.0
        raise FinancialDataError("currency instrument is missing for a non-zero amount")
    source_rate = db.require_instrument_rate(int(instrument_id))
    return numeric * source_rate / user_rate


def _descendant_tag_ids(db: HardenedDatabase, tag_id: str) -> list[str]:
    result: list[str] = []
    queue = [tag_id]
    seen: set[str] = set()
    conn = db.connect()
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        result.append(current)
        children = conn.execute(
            "SELECT id FROM tags WHERE parent = ?", (current,)
        ).fetchall()
        queue.extend(str(row["id"]) for row in children)
    return result


def _tag_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        tags = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(tag) for tag in tags] if isinstance(tags, list) else []


def _primary_tag(raw: str | None) -> str | None:
    tags = _tag_ids(raw)
    return tags[0] if tags else None


def _search_ids(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if (
        not isinstance(value, list)
        or len(value) > 100
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise InputValidationError(
            f"{field} must be an array of at most 100 non-empty IDs"
        )
    return list(dict.fromkeys(value))


def _encode_search_cursor(
    sort_by: str,
    sort_order: str,
    sort_value: str | float,
    tx_date: str,
    changed: int,
    transaction_id: str,
) -> str:
    payload = json.dumps(
        [1, sort_by, sort_order, sort_value, tx_date, changed, transaction_id],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_search_cursor(
    cursor: Any, sort_by: str, sort_order: str
) -> tuple[str | float, str, int, str]:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
        raise InputValidationError("cursor is invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            cursor + padding, altchars=b"-_", validate=True
        )
        value = json.loads(decoded)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise InputValidationError("cursor is invalid") from exc
    if (
        not isinstance(value, list)
        or len(value) != 7
        or value[:3] != [1, sort_by, sort_order]
        or not isinstance(value[4], str)
        or isinstance(value[5], bool)
        or not isinstance(value[5], int)
        or not isinstance(value[6], str)
        or not value[6]
    ):
        raise InputValidationError("cursor does not match the sort contract")
    sort_value = value[3]
    if sort_by == "date":
        if not isinstance(sort_value, str) or sort_value != value[4]:
            raise InputValidationError("cursor is invalid")
    elif (
        isinstance(sort_value, bool)
        or not isinstance(sort_value, (int, float))
        or not math.isfinite(float(sort_value))
    ):
        raise InputValidationError("cursor is invalid")
    if _encode_search_cursor(*value[1:]) != cursor:
        raise InputValidationError("cursor is invalid")
    return sort_value, value[4], value[5], value[6]


def _search_row_amount(row: Any, user_rate: float) -> float:
    if float(row["outcome"] or 0) > 0:
        return float(row["outcome"]) * float(row["outcome_rate"]) / user_rate
    if float(row["income"] or 0) > 0:
        return float(row["income"]) * float(row["income_rate"]) / user_rate
    return 0.0


def _bounded(
    value: int | None,
    field_name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Validate bounded pagination/aggregation arguments without clamping."""
    return bounded_int(
        value,
        field_name,
        default=default,
        minimum=minimum,
        maximum=maximum,
    )


def get_net_worth(db: HardenedDatabase) -> dict[str, Any]:
    """Calculate net worth, excluding accounts disabled in ZenMoney's balance."""
    user_currency_id, code, symbol, user_rate = _user_currency(db)
    rows = db.connect().execute(
        """
        SELECT a.id, a.title, a.type, a.balance, a.credit_limit,
               a.in_balance, a.savings, a.instrument,
               i.short_title AS currency, i.symbol AS currency_symbol
        FROM accounts a
        LEFT JOIN instruments i ON i.id = a.instrument
        WHERE COALESCE(a.archive, 0) = 0
        ORDER BY a.type, a.title
        """
    ).fetchall()

    buckets: dict[str, dict[str, Any]] = {
        "current": {"total": 0.0, "accounts": []},
        "savings": {"total": 0.0, "accounts": []},
        "loans": {"total": 0.0, "accounts": []},
        "debts": {"total": 0.0, "accounts": []},
    }
    excluded: list[dict[str, Any]] = []
    excluded_total = 0.0

    for row in rows:
        converted = _convert(
            db, row["balance"], row["instrument"], user_currency_id, user_rate
        )
        item = {
            "id": row["id"],
            "title": row["title"],
            "type": row["type"],
            "balance": float(row["balance"] or 0),
            "currency": row["currency"],
            "currency_symbol": row["currency_symbol"],
            "converted": round(converted, 2),
        }
        if not bool(row["in_balance"]):
            excluded.append(item)
            excluded_total += converted
            continue

        if row["type"] == "loan":
            bucket = "loans"
        elif row["type"] == "debt":
            bucket = "debts"
        elif row["type"] == "deposit" or bool(row["savings"]):
            bucket = "savings"
        else:
            bucket = "current"
        buckets[bucket]["accounts"].append(item)
        buckets[bucket]["total"] += converted

    included_total = sum(float(bucket["total"]) for bucket in buckets.values())
    for bucket in buckets.values():
        bucket["total"] = round(bucket["total"], 2)
    return {
        "net_worth": round(included_total, 2),
        "net_worth_all_accounts": round(included_total + excluded_total, 2),
        "currency": code,
        "currency_symbol": symbol,
        "breakdown": buckets,
        "out_of_balance": {
            "total": round(excluded_total, 2),
            "accounts": excluded,
        },
        "semantics": {
            "net_worth": "active accounts with in_balance=true",
            "net_worth_all_accounts": "also includes active accounts excluded from ZenMoney balance",
        },
    }


def get_liquidity(
    db: HardenedDatabase,
    target_amount: float | None = None,
) -> dict[str, Any]:
    """Separate own liquidity, savings, deposits, and borrowing capacity."""
    user_currency_id, code, symbol, user_rate = _user_currency(db)
    target = non_negative_number(target_amount, "target_amount", allow_none=True)
    rows = db.connect().execute(
        """
        SELECT a.id, a.title, a.type, a.balance, a.credit_limit,
               a.savings, a.instrument, i.short_title AS currency
        FROM accounts a
        LEFT JOIN instruments i ON i.id = a.instrument
        WHERE COALESCE(a.archive, 0)=0 AND COALESCE(a.in_balance, 0)=1
        ORDER BY a.type, a.title
        """
    ).fetchall()

    liquid_own = 0.0
    credit_available = 0.0
    accessible_savings = 0.0
    restricted_savings = 0.0
    breakdown = {
        "liquid_accounts": [],
        "credit_accounts": [],
        "accessible_savings": [],
        "restricted_savings": [],
    }

    for row in rows:
        balance = _convert(
            db, row["balance"], row["instrument"], user_currency_id, user_rate
        )
        credit_limit = _convert(
            db, row["credit_limit"], row["instrument"], user_currency_id, user_rate
        ) if row["credit_limit"] else 0.0
        item = {
            "id": row["id"],
            "title": row["title"],
            "type": row["type"],
            "balance": float(row["balance"] or 0),
            "currency": row["currency"],
            "balance_converted": round(balance, 2),
        }

        if row["type"] == "deposit":
            amount = max(0.0, balance)
            restricted_savings += amount
            item["available_assumption"] = "restricted_or_term_deposit"
            breakdown["restricted_savings"].append(item)
        elif bool(row["savings"]):
            amount = max(0.0, balance)
            accessible_savings += amount
            item["available_assumption"] = "accessible_savings_account"
            breakdown["accessible_savings"].append(item)
        elif row["type"] == "ccard" or credit_limit > 0:
            own = max(0.0, balance)
            available_credit = max(0.0, credit_limit + min(0.0, balance))
            liquid_own += own
            credit_available += available_credit
            item.update(
                {
                    "own_funds": round(own, 2),
                    "credit_limit": round(credit_limit, 2),
                    "credit_available": round(available_credit, 2),
                }
            )
            breakdown["credit_accounts"].append(item)
        elif row["type"] in ("cash", "checking", "emoney"):
            own = max(0.0, balance)
            liquid_own += own
            breakdown["liquid_accounts"].append(item)

    total_available = liquid_own + accessible_savings
    total_spendable_with_credit = total_available + credit_available
    result: dict[str, Any] = {
        "liquid_own": round(liquid_own, 2),
        "credit_available": round(credit_available, 2),
        "liquid_with_credit": round(liquid_own + credit_available, 2),
        "savings_accessible": round(accessible_savings, 2),
        "restricted_savings": round(restricted_savings, 2),
        "total_available": round(total_available, 2),
        "total_spendable_with_credit": round(total_spendable_with_credit, 2),
        "currency": code,
        "currency_symbol": symbol,
        "breakdown": breakdown,
        "semantics": {
            "total_available": "own liquid funds plus accessible savings; excludes deposits and credit",
            "credit_available": "borrowing capacity, not an asset",
            "restricted_savings": "reported separately because withdrawal terms are unknown",
        },
    }
    if target is not None:
        result["target_check"] = {
            "target": target,
            "affordable_from_liquid": liquid_own >= target,
            "affordable_with_accessible_savings": total_available >= target,
            "affordable_with_credit": total_spendable_with_credit >= target,
            "affordable_including_restricted_savings": (
                total_available + restricted_savings >= target
            ),
        }
    return result


def _budget_period(db: HardenedDatabase, month: str | None) -> tuple[str, date, date, date]:
    today = date.today()
    start_day = db.get_user_month_start_day()
    if month is None:
        if today.day >= start_day:
            year, month_number = today.year, today.month
        elif today.month == 1:
            year, month_number = today.year - 1, 12
        else:
            year, month_number = today.year, today.month - 1
    else:
        validate_period(month)
        if len(month) != 7:
            raise InputValidationError("month must be in YYYY-MM format")
        year, month_number = map(int, month.split("-"))

    import calendar

    start = date(year, month_number, min(start_day, calendar.monthrange(year, month_number)[1]))
    if month_number == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month_number + 1
    next_start = date(
        next_year,
        next_month,
        min(start_day, calendar.monthrange(next_year, next_month)[1]),
    )
    end = next_start - timedelta(days=1)
    return f"{year:04d}-{month_number:02d}", start, end, today


def _budget_status(planned: float, actual: float, progress: float) -> tuple[float | None, str, str]:
    if planned <= 0:
        if actual > 0:
            return None, "unbudgeted_spending", "not_applicable"
        return None, "not_budgeted", "not_applicable"
    pct = actual / planned * 100
    if pct >= 100:
        status = "overspent"
    elif pct >= 80:
        status = "warning"
    else:
        status = "on_track"
    spend_progress = actual / planned
    if spend_progress > progress * 1.1:
        pace = "ahead_of_pace"
    elif spend_progress < progress * 0.9:
        pace = "behind_pace"
    else:
        pace = "on_pace"
    return pct, status, pace


def _sum_expenses(
    db: HardenedDatabase,
    start: str,
    end: str,
    user_currency_id: int,
    user_rate: float,
    tag_ids: Iterable[str] | None = None,
    *,
    uncategorized_only: bool = False,
) -> float:
    query = """
        SELECT t.outcome, t.outcome_instrument
        FROM transactions t
        LEFT JOIN accounts a ON a.id=t.outcome_account
        WHERE COALESCE(t.deleted,0)=0
          AND COALESCE(t.hold,0)=0
          AND t.outcome>0 AND t.income=0
          AND t.date BETWEEN ? AND ?
          AND (a.in_balance=1 OR a.in_balance IS NULL)
    """
    params: list[Any] = [start, end]
    ids = list(tag_ids or [])
    if ids:
        query += f" AND EXISTS (SELECT 1 FROM json_each(t.tag) WHERE value IN ({','.join('?' for _ in ids)}))"
        params.extend(ids)
    elif uncategorized_only:
        query += " AND (t.tag IS NULL OR json_array_length(t.tag)=0)"
    rows = db.connect().execute(query, params).fetchall()
    return sum(
        _convert(db, row["outcome"], row["outcome_instrument"], user_currency_id, user_rate)
        for row in rows
    )


def check_budget_health(
    db: HardenedDatabase,
    month: str | None = None,
) -> dict[str, Any]:
    """Compare planned and actual spending without hiding zero-budget spend."""
    month_key, period_start, period_end, today = _budget_period(db, month)
    user_currency_id, code, _, user_rate = _user_currency(db)
    budget_date = f"{month_key}-01"
    rows = db.connect().execute(
        """
        SELECT b.tag, b.outcome, b.outcome_lock, t.title AS tag_title
        FROM budgets b LEFT JOIN tags t ON t.id=b.tag
        WHERE b.date=?
        ORDER BY b.outcome DESC
        """,
        (budget_date,),
    ).fetchall()

    days_total = (period_end - period_start).days + 1
    if today < period_start:
        elapsed, period_status = 0, "future"
    elif today > period_end:
        elapsed, period_status = days_total, "completed"
    else:
        elapsed, period_status = (today - period_start).days + 1, "current"
    remaining_days = max(0, days_total - elapsed)
    progress = elapsed / days_total if days_total else 0.0

    categories: list[dict[str, Any]] = []
    total_budget_value: float | None = None
    category_planned_total = 0.0
    covered_tag_ids: set[str] = set()
    uncategorized_covered = False
    budgeted_tag_ids = {
        str(row["tag"])
        for row in rows
        if row["tag"] not in (None, _TOTAL_BUDGET_TAG)
    }
    parent_by_budgeted_tag: dict[str, str | None] = {}
    if budgeted_tag_ids:
        placeholders = ",".join("?" for _ in budgeted_tag_ids)
        parent_by_budgeted_tag = {
            str(tag_row["id"]): tag_row["parent"]
            for tag_row in db.connect().execute(
                f"SELECT id,parent FROM tags WHERE id IN ({placeholders})",
                list(budgeted_tag_ids),
            ).fetchall()
        }
    for row in rows:
        tag_id = row["tag"]
        if tag_id == _TOTAL_BUDGET_TAG:
            total_budget_value = float(row["outcome"] or 0)
            continue
        all_ids = _descendant_tag_ids(db, tag_id) if tag_id else []
        # When a child has its own budget, the parent scope excludes that child
        # subtree. This makes category actuals mutually exclusive.
        ids = [
            candidate
            for candidate in all_ids
            if candidate == tag_id or candidate not in budgeted_tag_ids
        ]
        if tag_id:
            covered_tag_ids.update(all_ids)
        else:
            uncategorized_covered = True
        planned = float(row["outcome"] or 0)
        if not bool(row["outcome_lock"]):
            marker_query = """
                SELECT rm.outcome,
                       COALESCE(rm.outcome_instrument, a.instrument) AS instrument
                FROM reminder_markers rm
                LEFT JOIN accounts a ON a.id=rm.outcome_account
                WHERE rm.state='planned'
                  AND rm.date BETWEEN ? AND ?
            """
            marker_params: list[Any] = [period_start.isoformat(), period_end.isoformat()]
            if ids:
                marker_query += f" AND EXISTS (SELECT 1 FROM json_each(rm.tag) WHERE value IN ({','.join('?' for _ in ids)}))"
                marker_params.extend(ids)
            elif tag_id is None:
                marker_query += " AND (rm.tag IS NULL OR json_array_length(rm.tag)=0)"
            marker_rows = db.connect().execute(marker_query, marker_params).fetchall()
            planned += sum(
                _convert(db, marker["outcome"], marker["instrument"], user_currency_id, user_rate)
                for marker in marker_rows
            )
        actual = _sum_expenses(
            db,
            period_start.isoformat(),
            period_end.isoformat(),
            user_currency_id,
            user_rate,
            ids if tag_id else None,
            uncategorized_only=tag_id is None,
        )
        pct, status, pace = _budget_status(planned, actual, progress)
        item: dict[str, Any] = {
            "tag_id": tag_id,
            "name": row["tag_title"] or "Uncategorized",
            "planned": round(planned, 2),
            "actual": round(actual, 2),
            "remaining": round(planned - actual, 2),
            "pct_used": round(pct, 1) if pct is not None else None,
            "daily_remaining": (
                round(max(0.0, planned - actual) / remaining_days, 2)
                if remaining_days and planned > 0
                else 0
            ),
            "status": status,
            "pace": pace,
        }
        if status == "unbudgeted_spending":
            item["insight"] = f"Unbudgeted spending: {round(actual, 2)} {code}"
        categories.append(item)
        if tag_id is None or parent_by_budgeted_tag.get(str(tag_id)) not in budgeted_tag_ids:
            category_planned_total += planned

    # Surface spending that is not covered by any budget row. A parent
    # budget covers its descendants; uncategorized spending is covered only by
    # an explicit NULL-tag budget.
    expense_rows = db.connect().execute(
        """
        SELECT t.outcome, t.outcome_instrument, t.tag
        FROM transactions t
        LEFT JOIN accounts a ON a.id=t.outcome_account
        WHERE COALESCE(t.deleted,0)=0
          AND COALESCE(t.hold,0)=0
          AND t.outcome>0 AND t.income=0
          AND t.date BETWEEN ? AND ?
          AND (a.in_balance=1 OR a.in_balance IS NULL)
        """,
        (period_start.isoformat(), period_end.isoformat()),
    ).fetchall()
    uncovered: dict[str | None, float] = {}
    for expense in expense_rows:
        tag_ids = _tag_ids(expense["tag"])
        if not tag_ids and uncategorized_covered:
            continue
        if any(tag_id in covered_tag_ids for tag_id in tag_ids):
            continue
        tag_id = tag_ids[0] if tag_ids else None
        uncovered[tag_id] = uncovered.get(tag_id, 0.0) + _convert(
            db,
            expense["outcome"],
            expense["outcome_instrument"],
            user_currency_id,
            user_rate,
        )

    uncovered_titles: dict[str, str] = {}
    uncovered_ids = [tag_id for tag_id in uncovered if tag_id]
    if uncovered_ids:
        placeholders = ",".join("?" for _ in uncovered_ids)
        uncovered_titles = {
            row["id"]: row["title"]
            for row in db.connect().execute(
                f"SELECT id,title FROM tags WHERE id IN ({placeholders})",
                uncovered_ids,
            ).fetchall()
        }
    for tag_id, actual in uncovered.items():
        if actual <= 0:
            continue
        categories.append(
            {
                "tag_id": tag_id,
                "name": uncovered_titles.get(tag_id, tag_id) if tag_id else "Uncategorized",
                "planned": 0.0,
                "actual": round(actual, 2),
                "remaining": round(-actual, 2),
                "pct_used": None,
                "daily_remaining": 0,
                "status": "unbudgeted_spending",
                "pace": "not_applicable",
                "insight": f"Unbudgeted spending: {round(actual, 2)} {code}",
            }
        )

    overall_actual = _sum_expenses(
        db,
        period_start.isoformat(),
        period_end.isoformat(),
        user_currency_id,
        user_rate,
    )
    overall_planned = total_budget_value if total_budget_value is not None else category_planned_total
    overall_pct, overall_status, overall_pace = _budget_status(
        overall_planned, overall_actual, progress
    )
    categories.sort(
        key=lambda item: (
            item["status"] != "unbudgeted_spending",
            -(item["pct_used"] or 0),
        )
    )
    return {
        "month": month_key,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "status": period_status,
        "days_elapsed": elapsed,
        "days_total": days_total,
        "currency": code,
        "categories": categories,
        "overall": {
            "planned": round(overall_planned, 2),
            "actual": round(overall_actual, 2),
            "remaining": round(overall_planned - overall_actual, 2),
            "pct_used": round(overall_pct, 1) if overall_pct is not None else None,
            "status": overall_status,
            "pace": overall_pace,
        },
    }


def get_debts(db: HardenedDatabase) -> dict[str, Any]:
    """Use debt-account balances as truth and history only for attribution."""
    user_currency_id, code, _, user_rate = _user_currency(db)
    debt_accounts = db.connect().execute(
        """
        SELECT id,title,balance,instrument
        FROM accounts
        WHERE type='debt' AND COALESCE(archive,0)=0
        ORDER BY title
        """
    ).fetchall()
    counterparties: dict[str, dict[str, Any]] = {}
    account_results: list[dict[str, Any]] = []
    total_positive = 0.0
    total_negative = 0.0

    for account in debt_accounts:
        reported = _convert(
            db, account["balance"], account["instrument"], user_currency_id, user_rate
        )
        if reported >= 0:
            total_positive += reported
        else:
            total_negative += abs(reported)
        rows = db.connect().execute(
            """
            SELECT t.id,t.date,t.income,t.outcome,t.income_instrument,
                   t.outcome_instrument,t.income_account,t.outcome_account,
                   t.merchant,t.payee,t.comment,m.title AS merchant_title
            FROM transactions t
            LEFT JOIN merchants m ON m.id=t.merchant
            WHERE COALESCE(t.deleted,0)=0 AND COALESCE(t.hold,0)=0
              AND (t.income_account=? OR t.outcome_account=?)
            ORDER BY t.date DESC,t.changed DESC
            """,
            (account["id"], account["id"]),
        ).fetchall()
        allocated = 0.0
        for row in rows:
            if row["income_account"] == account["id"]:
                signed = _convert(
                    db, row["income"], row["income_instrument"], user_currency_id, user_rate
                )
                kind = "lent_or_receivable_increase"
            elif row["outcome_account"] == account["id"]:
                signed = -_convert(
                    db, row["outcome"], row["outcome_instrument"], user_currency_id, user_rate
                )
                kind = "returned_or_liability_increase"
            else:
                continue
            allocated += signed
            name = row["merchant_title"] or row["payee"] or "Unknown"
            cp = counterparties.setdefault(
                name,
                {"counterparty": name, "merchant_id": row["merchant"], "net_amount": 0.0, "transactions": []},
            )
            cp["net_amount"] += signed
            cp["transactions"].append(
                {
                    "id": row["id"],
                    "date": row["date"],
                    "amount": round(abs(signed), 2),
                    "signed_amount": round(signed, 2),
                    "type": kind,
                    "comment": row["comment"],
                    "account_id": account["id"],
                }
            )

        gap = reported - allocated
        if abs(gap) >= 0.005:
            cp = counterparties.setdefault(
                "Unallocated balance",
                {"counterparty": "Unallocated balance", "merchant_id": None, "net_amount": 0.0, "transactions": []},
            )
            cp["net_amount"] += gap
        account_results.append(
            {
                "id": account["id"],
                "title": account["title"],
                "reported_balance": round(reported, 2),
                "history_allocated": round(allocated, 2),
                "reconciliation_gap": round(gap, 2),
            }
        )

    formatted: list[dict[str, Any]] = []
    for cp in counterparties.values():
        amount = float(cp["net_amount"])
        formatted.append(
            {
                **cp,
                "net_amount": round(amount, 2),
                "status": (
                    "they_owe_you" if amount > 0 else "you_owe_them" if amount < 0 else "settled"
                ),
                "last_activity": cp["transactions"][0]["date"] if cp["transactions"] else None,
                "transactions": cp["transactions"][:10],
            }
        )
    formatted.sort(key=lambda item: abs(item["net_amount"]), reverse=True)
    return {
        "currency": code,
        "summary": {
            "total_owed_to_you": round(total_positive, 2),
            "total_you_owe": round(total_negative, 2),
            "net_position": round(total_positive - total_negative, 2),
        },
        "accounts": account_results,
        "by_counterparty": formatted,
        "semantics": "account balances are authoritative; history is attribution and may have a reconciliation gap",
    }


def get_account_flow(
    db: HardenedDatabase,
    account_id: str,
    period: str = "this_month",
    start_date: str | None = None,
    end_date: str | None = None,
    include_holds: bool = False,
) -> dict[str, Any]:
    """Return signed account-currency movements, including transfers."""
    start, end = resolve_date_range(period, start_date, end_date)
    user_currency_id, user_code, _, user_rate = _user_currency(db)
    account = db.connect().execute(
        """
        SELECT a.id,a.title,a.type,a.balance,a.instrument,
               i.short_title AS currency,i.symbol AS currency_symbol
        FROM accounts a LEFT JOIN instruments i ON i.id=a.instrument
        WHERE a.id=?
        """,
        (account_id,),
    ).fetchone()
    if account is None:
        raise ValueError(f"Account {account_id} not found")
    account_rate = db.require_instrument_rate(account["instrument"])
    balance_converted = float(account["balance"] or 0) * account_rate / user_rate

    query = """
        SELECT t.id,t.date,t.income,t.outcome,t.hold,t.comment,
               t.income_account,t.outcome_account,t.income_instrument,
               t.outcome_instrument,t.tag,t.merchant,t.payee,
               m.title AS merchant_title,tag.title AS tag_title,
               ia.title AS income_account_title,oa.title AS outcome_account_title
        FROM transactions t
        LEFT JOIN merchants m ON m.id=t.merchant
        LEFT JOIN tags tag ON tag.id=json_extract(t.tag,'$[0]')
        LEFT JOIN accounts ia ON ia.id=t.income_account
        LEFT JOIN accounts oa ON oa.id=t.outcome_account
        WHERE COALESCE(t.deleted,0)=0
          AND t.date BETWEEN ? AND ?
          AND (t.income_account=? OR t.outcome_account=?)
    """
    params: list[Any] = [start, end, account_id, account_id]
    if not include_holds:
        query += " AND COALESCE(t.hold,0)=0"
    query += " ORDER BY t.date DESC,t.changed DESC"
    rows = db.connect().execute(query, params).fetchall()

    transactions: list[dict[str, Any]] = []
    by_category: dict[tuple[str, str], dict[str, Any]] = {}
    pure_income = pure_outcome = transfer_in = transfer_out = 0.0
    net_change = 0.0
    net_change_converted = 0.0

    for row in rows:
        income, outcome = float(row["income"] or 0), float(row["outcome"] or 0)
        if income > 0 and outcome == 0 and row["income_account"] == account_id:
            kind, raw, signed, instrument = "income", income, income, row["income_instrument"]
        elif outcome > 0 and income == 0 and row["outcome_account"] == account_id:
            kind, raw, signed, instrument = "outcome", outcome, -outcome, row["outcome_instrument"]
        elif income > 0 and outcome > 0 and row["income_account"] == account_id and row["outcome_account"] != account_id:
            kind, raw, signed, instrument = "transfer_in", income, income, row["income_instrument"]
        elif income > 0 and outcome > 0 and row["outcome_account"] == account_id and row["income_account"] != account_id:
            kind, raw, signed, instrument = "transfer_out", outcome, -outcome, row["outcome_instrument"]
        else:
            continue

        source_rate = db.require_instrument_rate(instrument)
        converted_abs = raw * source_rate / user_rate
        account_amount = raw * source_rate / account_rate
        signed = account_amount if signed >= 0 else -account_amount
        converted_signed = converted_abs if signed >= 0 else -converted_abs
        if kind == "income":
            pure_income += account_amount
        elif kind == "outcome":
            pure_outcome += account_amount
        elif kind == "transfer_in":
            transfer_in += account_amount
        else:
            transfer_out += account_amount
        net_change += signed
        net_change_converted += converted_signed
        category = row["tag_title"] or "Uncategorized"
        if kind in ("income", "outcome"):
            key = (category, kind)
            bucket = by_category.setdefault(
                key, {"category": category, "type": kind, "total": 0.0, "count": 0}
            )
            bucket["total"] += account_amount
            bucket["count"] += 1
        counterparty = (
            row["outcome_account_title"] if kind == "transfer_in"
            else row["income_account_title"] if kind == "transfer_out"
            else None
        )
        transactions.append(
            {
                "id": row["id"],
                "date": row["date"],
                "type": kind,
                "amount": round(account_amount, 2),
                "currency": account["currency"],
                "signed_change": round(signed, 2),
                "amount_converted": round(converted_abs, 2),
                "signed_change_converted": round(converted_signed, 2),
                "converted_currency": user_code,
                "category": row["tag_title"],
                "payee": row["merchant_title"] or row["payee"],
                "counterparty": counterparty,
                "comment": row["comment"],
                "hold": bool(row["hold"]),
            }
        )

    category_list = list(by_category.values())
    for item in category_list:
        item["total"] = round(item["total"], 2)
    category_list.sort(key=lambda item: item["total"], reverse=True)
    return {
        "account": {
            "id": account["id"],
            "title": account["title"],
            "type": account["type"],
            "balance": round(float(account["balance"] or 0), 2),
            "currency": account["currency"],
            "currency_symbol": account["currency_symbol"],
            "balance_converted": round(balance_converted, 2),
            "converted_currency": user_code,
        },
        "period": {"start": start, "end": end},
        "summary": {
            "total_income": round(pure_income, 2),
            "total_outcome": round(pure_outcome, 2),
            "transfer_in": round(transfer_in, 2),
            "transfer_out": round(transfer_out, 2),
            "net_change": round(net_change, 2),
            "net_change_converted": round(net_change_converted, 2),
            "converted_currency": user_code,
            "transaction_count": len(transactions),
            "by_category": category_list,
        },
        "transactions": transactions[:50],
        "returned_count": min(50, len(transactions)),
        "total_count": len(transactions),
    }


def analyze_spending(
    db: HardenedDatabase,
    period: str = "this_month",
    category_id: str | None = None,
    top_n: int = 10,
    include_transfers: bool = False,
    include_holds: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
    group_by: str = "category",
) -> dict[str, Any]:
    """Analyze pure expenses; transfers must use ``analyze_transfers``."""
    if include_transfers:
        raise InputValidationError(
            "analyze_spending does not mix transfers with expenses; use analyze_transfers"
        )
    if group_by not in ("category", "merchant"):
        raise InputValidationError("group_by must be category or merchant")
    top_n = _bounded(top_n, "top_n", 10, 1, 100)
    start, end = resolve_date_range(period, start_date, end_date)
    user_currency_id, code, _, user_rate = _user_currency(db)
    query = """
        SELECT t.outcome,t.outcome_instrument,t.tag,t.hold,t.merchant,t.payee,
               m.title AS merchant_title
        FROM transactions t
        LEFT JOIN accounts a ON a.id=t.outcome_account
        LEFT JOIN merchants m ON m.id=t.merchant
        WHERE COALESCE(t.deleted,0)=0
          AND t.outcome>0 AND t.income=0
          AND t.date BETWEEN ? AND ?
          AND (a.in_balance=1 OR a.in_balance IS NULL)
    """
    params: list[Any] = [start, end]
    rows = db.connect().execute(query, params).fetchall()
    allowed = set(_descendant_tag_ids(db, category_id)) if category_id else None
    categories: dict[str | None, dict[str, Any]] = {}
    merchants: dict[str, dict[str, Any]] = {}
    holds_excluded = {"amount": 0.0, "count": 0}
    for row in rows:
        tag_id = _primary_tag(row["tag"])
        if allowed is not None and tag_id not in allowed:
            continue
        amount = _convert(
            db, row["outcome"], row["outcome_instrument"], user_currency_id, user_rate
        )
        if bool(row["hold"]) and not include_holds:
            holds_excluded["amount"] += amount
            holds_excluded["count"] += 1
            continue
        category = categories.setdefault(tag_id, {"tag_id": tag_id, "amount": 0.0, "count": 0})
        category["amount"] += amount
        category["count"] += 1
        name = row["merchant_title"] or row["payee"] or "Unknown"
        key = row["merchant"] or name
        merchant = merchants.setdefault(
            key,
            {"merchant_id": row["merchant"], "name": name, "amount": 0.0, "count": 0},
        )
        merchant["amount"] += amount
        merchant["count"] += 1
    total = sum(item["amount"] for item in categories.values())
    if group_by == "merchant":
        output = [
            {
                **item,
                "amount": round(item["amount"], 2),
                "share_pct": round(item["amount"] / total * 100, 1) if total else 0,
                "avg_check": round(item["amount"] / item["count"], 2),
            }
            for item in merchants.values()
        ]
        output.sort(key=lambda item: item["amount"], reverse=True)
        return {
            "period": {"start": start, "end": end},
            "total_outcome": round(total, 2),
            "currency": code,
            "group_by": "merchant",
            "merchants": output[:top_n],
            "returned_count": min(top_n, len(output)),
            "total_merchants": len(output),
            "holds_excluded": (
                {
                    "amount": round(holds_excluded["amount"], 2),
                    "count": holds_excluded["count"],
                }
                if holds_excluded["count"]
                else None
            ),
        }
    tag_ids = [tag for tag in categories if tag]
    titles: dict[str, tuple[str, str | None]] = {}
    if tag_ids:
        placeholders = ",".join("?" for _ in tag_ids)
        for row in db.connect().execute(
            f"SELECT id,title,parent FROM tags WHERE id IN ({placeholders})", tag_ids
        ).fetchall():
            titles[row["id"]] = (row["title"], row["parent"])
    output = []
    uncategorized = None
    for tag_id, item in categories.items():
        if tag_id is None:
            uncategorized = {"amount": round(item["amount"], 2), "count": item["count"]}
            continue
        title, parent = titles.get(tag_id, (tag_id, None))
        entry = {
            "tag_id": tag_id,
            "name": title,
            "amount": round(item["amount"], 2),
            "share_pct": round(item["amount"] / total * 100, 1) if total else 0,
            "count": item["count"],
            "avg_check": round(item["amount"] / item["count"], 2),
        }
        if parent:
            parent_row = db.connect().execute("SELECT title FROM tags WHERE id=?", (parent,)).fetchone()
            if parent_row:
                entry["parent_category"] = parent_row["title"]
        output.append(entry)
    output.sort(key=lambda item: item["amount"], reverse=True)
    return {
        "period": {"start": start, "end": end},
        "total_outcome": round(total, 2),
        "currency": code,
        "categories": output[:top_n],
        "returned_count": min(top_n, len(output)),
        "total_categories": len(output),
        "uncategorized": uncategorized,
        "holds_excluded": (
            {
                "amount": round(holds_excluded["amount"], 2),
                "count": holds_excluded["count"],
            }
            if holds_excluded["count"]
            else None
        ),
    }


def search_transactions(
    db: HardenedDatabase,
    period: str | None = None,
    category_id: str | None = None,
    account_id: str | None = None,
    merchant_id: str | None = None,
    payee_search: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    tx_type: str | None = None,
    limit: int = 50,
    start_date: str | None = None,
    end_date: str | None = None,
    cursor: str | None = None,
    sort_by: str = "date",
    sort_order: str = "desc",
    category_state: str = "any",
    category_ids: list[str] | None = None,
    account_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Search one stable keyset page with a hard 200-row response bound."""
    applied_limit = _bounded(limit, "limit", 50, 1, 200)
    lower, upper = validate_amount_range(min_amount, max_amount)
    if tx_type not in (None, "income", "outcome", "transfer"):
        raise InputValidationError("type must be income, outcome, or transfer")
    if sort_by not in {"date", "amount"}:
        raise InputValidationError("sort_by must be date or amount")
    if sort_order not in {"asc", "desc"}:
        raise InputValidationError("sort_order must be asc or desc")
    if category_state not in {"any", "categorized", "uncategorized"}:
        raise InputValidationError(
            "category_state must be any, categorized, or uncategorized"
        )
    categories = _search_ids(category_ids, "category_ids")
    accounts = _search_ids(account_ids, "account_ids")
    if category_id is not None:
        if not isinstance(category_id, str) or not category_id:
            raise InputValidationError("category_id must be a non-empty ID")
        categories.insert(0, category_id)
    if account_id is not None:
        if not isinstance(account_id, str) or not account_id:
            raise InputValidationError("account_id must be a non-empty ID")
        accounts.insert(0, account_id)
    categories = list(dict.fromkeys(categories))
    accounts = list(dict.fromkeys(accounts))
    user_currency_id, user_code, _, user_rate = _user_currency(db)
    amount_expr = """CASE
          WHEN t.outcome>0 THEN t.outcome * oi.rate / ?
          WHEN t.income>0 THEN t.income * ii.rate / ?
          ELSE 0
        END"""
    query = """
        SELECT t.id,t.date,t.income,t.outcome,t.hold,t.income_instrument,
               t.outcome_instrument,t.income_account,t.outcome_account,t.tag,
               t.merchant,t.payee,t.original_payee,t.comment,t.changed,
               m.title AS merchant_title,ia.title AS income_account_title,
               oa.title AS outcome_account_title,ii.short_title AS income_currency,
               oi.short_title AS outcome_currency,ii.rate AS income_rate,
               oi.rate AS outcome_rate
        FROM transactions t
        LEFT JOIN merchants m ON m.id=t.merchant
        LEFT JOIN accounts ia ON ia.id=t.income_account
        LEFT JOIN accounts oa ON oa.id=t.outcome_account
        LEFT JOIN instruments ii ON ii.id=t.income_instrument
        LEFT JOIN instruments oi ON oi.id=t.outcome_instrument
        WHERE COALESCE(t.deleted,0)=0
    """
    params: list[Any] = []
    if start_date is not None or end_date is not None or period is not None:
        start, end = resolve_date_range(period or "this_month", start_date, end_date)
        query += " AND t.date BETWEEN ? AND ?"
        params.extend([start, end])
    if categories:
        expanded = list(
            dict.fromkeys(
                descendant
                for category in categories
                for descendant in _descendant_tag_ids(db, category)
            )
        )
        placeholders = ",".join("?" for _ in expanded)
        query += (
            " AND EXISTS (SELECT 1 FROM json_each(t.tag) "
            f"WHERE value IN ({placeholders}))"
        )
        params.extend(expanded)
    if accounts:
        placeholders = ",".join("?" for _ in accounts)
        query += (
            f" AND (t.income_account IN ({placeholders}) "
            f"OR t.outcome_account IN ({placeholders}))"
        )
        params.extend([*accounts, *accounts])
    if category_state == "categorized":
        query += " AND t.tag IS NOT NULL AND COALESCE(json_array_length(t.tag),0)>0"
    elif category_state == "uncategorized":
        query += " AND (t.tag IS NULL OR COALESCE(json_array_length(t.tag),0)=0)"
    if merchant_id:
        query += " AND t.merchant=?"
        params.append(merchant_id)
    if payee_search:
        pattern = f"%{payee_search}%"
        query += " AND (t.payee LIKE ? OR t.original_payee LIKE ? OR t.comment LIKE ? OR m.title LIKE ?)"
        params.extend([pattern] * 4)
    if tx_type == "income":
        query += " AND t.income>0 AND t.outcome=0"
    elif tx_type == "outcome":
        query += " AND t.outcome>0 AND t.income=0"
    elif tx_type == "transfer":
        query += " AND t.income>0 AND t.outcome>0"

    # Validate all candidate-side rates before numeric predicates are
    # applied. Otherwise a NULL/zero rate can silently make an operation vanish
    # from min/max searches instead of reporting unusable financial data.
    invalid_rate_query = f"""
        SELECT id,
               CASE WHEN outcome>0 THEN outcome_instrument ELSE income_instrument END AS instrument_id
        FROM ({query}) AS candidates
        WHERE (outcome>0 AND (outcome_instrument IS NULL OR outcome_rate IS NULL OR outcome_rate<=0))
           OR (income>0 AND (income_instrument IS NULL OR income_rate IS NULL OR income_rate<=0))
        LIMIT 1
    """
    invalid_rate = db.connect().execute(invalid_rate_query, params).fetchone()
    if invalid_rate is not None:
        db.require_instrument_rate(invalid_rate["instrument_id"])

    # Thresholds and amount sorting are expressed in the user's currency.
    if lower is not None:
        query += f" AND ({amount_expr}) >= ?"
        params.extend([user_rate, user_rate, lower])
    if upper is not None:
        query += f" AND ({amount_expr}) <= ?"
        params.extend([user_rate, user_rate, upper])
    count_query = f"SELECT COUNT(*) AS total FROM ({query})"
    total = db.connect().execute(count_query, params).fetchone()["total"]

    if cursor is not None:
        sort_value, cursor_date, cursor_changed, cursor_id = _decode_search_cursor(
            cursor, sort_by, sort_order
        )
        comparison = ">" if sort_order == "asc" else "<"
        if sort_by == "date":
            query += f""" AND (
                t.date {comparison} ?
                OR (t.date=? AND COALESCE(t.changed,0) {comparison} ?)
                OR (t.date=? AND COALESCE(t.changed,0)=? AND t.id {comparison} ?)
            )"""
            params.extend(
                [
                    cursor_date,
                    cursor_date,
                    cursor_changed,
                    cursor_date,
                    cursor_changed,
                    cursor_id,
                ]
            )
        else:
            clauses = [
                f"({amount_expr}) {comparison} ?",
                f"(({amount_expr}) = ? AND t.date {comparison} ?)",
                f"(({amount_expr}) = ? AND t.date=? AND COALESCE(t.changed,0) {comparison} ?)",
                f"(({amount_expr}) = ? AND t.date=? AND COALESCE(t.changed,0)=? AND t.id {comparison} ?)",
            ]
            query += " AND (" + " OR ".join(clauses) + ")"
            params.extend([user_rate, user_rate, sort_value])
            params.extend([user_rate, user_rate, sort_value, cursor_date])
            params.extend(
                [
                    user_rate,
                    user_rate,
                    sort_value,
                    cursor_date,
                    cursor_changed,
                ]
            )
            params.extend(
                [
                    user_rate,
                    user_rate,
                    sort_value,
                    cursor_date,
                    cursor_changed,
                    cursor_id,
                ]
            )

    direction = sort_order.upper()
    if sort_by == "amount":
        query += (
            f" ORDER BY ({amount_expr}) {direction},t.date {direction},"
            f"COALESCE(t.changed,0) {direction},t.id {direction} LIMIT ?"
        )
        params.extend([user_rate, user_rate])
    else:
        query += (
            f" ORDER BY t.date {direction},COALESCE(t.changed,0) {direction},"
            f"t.id {direction} LIMIT ?"
        )
    rows = db.connect().execute(query, [*params, applied_limit + 1]).fetchall()
    has_more = len(rows) > applied_limit
    page = rows[:applied_limit]

    tag_ids: set[str] = set()
    for row in page:
        tag = _primary_tag(row["tag"])
        if tag:
            tag_ids.add(tag)
    titles: dict[str, str] = {}
    if tag_ids:
        placeholders = ",".join("?" for _ in tag_ids)
        titles = {
            row["id"]: row["title"]
            for row in db.connect().execute(
                f"SELECT id,title FROM tags WHERE id IN ({placeholders})", list(tag_ids)
            ).fetchall()
        }

    transactions: list[dict[str, Any]] = []
    for row in page:
        income, outcome = float(row["income"] or 0), float(row["outcome"] or 0)
        if income > 0 and outcome == 0:
            kind, raw, instrument, raw_currency, account = (
                "income", income, row["income_instrument"], row["income_currency"], row["income_account_title"]
            )
        elif outcome > 0 and income == 0:
            kind, raw, instrument, raw_currency, account = (
                "outcome", outcome, row["outcome_instrument"], row["outcome_currency"], row["outcome_account_title"]
            )
        else:
            kind, raw, instrument, raw_currency, account = (
                "transfer",
                outcome,
                row["outcome_instrument"],
                row["outcome_currency"],
                f"{row['outcome_account_title']} → {row['income_account_title']}",
            )
        converted = _convert(db, raw, instrument, user_currency_id, user_rate)
        tag = _primary_tag(row["tag"])
        payee = row["merchant_title"] or row["payee"] or row["comment"]
        transactions.append(
            {
                "id": row["id"],
                "date": row["date"],
                "type": kind,
                "amount": round(raw, 2),
                "currency": raw_currency,
                "amount_converted": round(converted, 2),
                "converted_currency": user_code,
                "account": account,
                "category_id": tag,
                "category": titles.get(tag, tag) if tag else None,
                "payee": payee,
                "comment": row["comment"] if row["comment"] != payee else None,
                "hold": bool(row["hold"]),
            }
        )
    next_cursor = None
    if has_more and page:
        last = page[-1]
        sort_value = (
            last["date"]
            if sort_by == "date"
            else _search_row_amount(last, user_rate)
        )
        next_cursor = _encode_search_cursor(
            sort_by,
            sort_order,
            sort_value,
            last["date"],
            int(last["changed"] or 0),
            str(last["id"]),
        )
    return {
        "transactions": transactions,
        "returned_count": len(transactions),
        "total_matching": int(total),
        "limit_applied": applied_limit,
        "next_cursor": next_cursor,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }


def convert_currency(
    db: HardenedDatabase,
    amount: float,
    from_currency: str,
    to_currency: str,
) -> dict[str, Any]:
    """Convert using only positive rates present in the synchronized cache."""
    numeric = non_negative_number(amount, "amount")
    assert numeric is not None
    source_code = validate_currency_code(from_currency, "from_currency")
    target_code = validate_currency_code(to_currency, "to_currency")
    conn = db.connect()
    source = conn.execute(
        "SELECT id,short_title,symbol FROM instruments WHERE UPPER(short_title)=UPPER(?)",
        (source_code,),
    ).fetchone()
    target = conn.execute(
        "SELECT id,short_title,symbol FROM instruments WHERE UPPER(short_title)=UPPER(?)",
        (target_code,),
    ).fetchone()
    if source is None:
        raise CurrencyRateError(f"currency {source_code} is not present in the cache")
    if target is None:
        raise CurrencyRateError(f"currency {target_code} is not present in the cache")
    source_rate = db.require_instrument_rate(source["id"])
    target_rate = db.require_instrument_rate(target["id"])
    rate = source_rate / target_rate
    return {
        "from": {"amount": numeric, "currency": source["short_title"], "symbol": source["symbol"]},
        "to": {"amount": round(numeric * rate, 2), "currency": target["short_title"], "symbol": target["symbol"]},
        "rate": round(rate, 6),
        "inverse_rate": round(1 / rate, 6),
        "rate_source": "ZenMoney synchronized instrument data",
    }


def get_exchange_rates(
    db: HardenedDatabase,
    currencies: list[str] | None = None,
) -> dict[str, Any]:
    """Return synchronized positive rates without claiming an upstream provider."""
    _, user_code, _, _ = _user_currency(db)
    if currencies is None:
        codes = [
            row["short_title"]
            for row in db.connect().execute(
                """
                SELECT DISTINCT i.short_title
                FROM accounts a JOIN instruments i ON i.id=a.instrument
                WHERE COALESCE(a.archive,0)=0
                ORDER BY i.short_title
                """
            ).fetchall()
        ]
    else:
        codes = sorted(validate_currency_list(currencies))
    entries = []
    rates: dict[str, float] = {}
    for code in codes:
        row = db.connect().execute(
            "SELECT id,short_title,symbol,title FROM instruments WHERE short_title=?",
            (code,),
        ).fetchone()
        if row is None:
            raise CurrencyRateError(f"currency {code} is not present in the cache")
        rate = db.require_instrument_rate(row["id"])
        rates[code] = rate
        entries.append(
            {"currency": code, "symbol": row["symbol"], "title": row["title"], "rate_to_rub": rate}
        )
    cross = {
        source: {
            target: round(source_rate / target_rate, 6)
            for target, target_rate in rates.items()
            if target != source
        }
        for source, source_rate in rates.items()
    }
    return {
        "user_currency": user_code,
        "currencies": entries,
        "cross_rates": cross,
        "rate_source": "ZenMoney synchronized instrument data",
        "note": "The ZenMoney API supplies rates relative to RUB; this server does not infer the upstream provider.",
    }

# ---------------------------------------------------------------------------
# Strict wrappers for analytics that retain their upstream implementation.
# ---------------------------------------------------------------------------

_legacy_analytics: Any | None = None


def configure_legacy_analytics(module: Any) -> None:
    """Bind the upstream analytics module used by validated wrappers."""
    global _legacy_analytics
    _legacy_analytics = module


def _legacy(name: str) -> Any:
    if _legacy_analytics is None:
        raise RuntimeError("legacy analytics module has not been configured")
    function = getattr(_legacy_analytics, name, None)
    if function is None:
        raise RuntimeError(f"legacy analytics function {name} is unavailable")
    return function


def analyze_income(
    db: Any,
    period: str = "this_month",
    top_n: int = 10,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    validate_period(period)
    resolve_date_range(period, start_date, end_date)
    top_n = _bounded(top_n, "top_n", 10, 1, 100)
    return _legacy("analyze_income")(
        db,
        period=period,
        top_n=top_n,
        start_date=start_date,
        end_date=end_date,
    )


def analyze_merchants(
    db: Any,
    period: str = "this_month",
    category_id: str | None = None,
    top_n: int = 10,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    validate_period(period)
    resolve_date_range(period, start_date, end_date)
    top_n = _bounded(top_n, "top_n", 10, 1, 100)
    return _legacy("analyze_merchants")(
        db,
        period=period,
        category_id=category_id,
        top_n=top_n,
        start_date=start_date,
        end_date=end_date,
    )


def analyze_transfers(
    db: Any,
    period: str = "this_month",
    top_n: int = 15,
) -> dict[str, Any]:
    validate_period(period)
    top_n = _bounded(top_n, "top_n", 15, 1, 100)
    return _legacy("analyze_transfers")(db, period=period, top_n=top_n)


def analyze_trends(
    db: Any,
    months: int = 6,
    category_id: str | None = None,
    metric: str = "outcome",
) -> dict[str, Any]:
    months = bounded_int(months, "months", default=6, minimum=1, maximum=60)
    if metric not in ("outcome", "income", "savings_rate", "net_cashflow"):
        raise InputValidationError(
            "metric must be outcome, income, savings_rate, or net_cashflow"
        )
    return _legacy("analyze_trends")(
        db, months=months, category_id=category_id, metric=metric
    )


def detect_recurring(
    db: Any,
    lookback_months: int = 3,
    tolerance_pct: int = 10,
) -> dict[str, Any]:
    lookback_months = bounded_int(lookback_months, "lookback_months", default=3, minimum=1, maximum=60)
    tolerance_pct = bounded_int(tolerance_pct, "tolerance_pct", default=10, minimum=0, maximum=100)
    return _legacy("detect_recurring")(
        db,
        lookback_months=lookback_months,
        tolerance_pct=tolerance_pct,
    )


def detect_anomalies(
    db: Any,
    period: str = "this_month",
    category_id: str | None = None,
    z_threshold: float = 2.0,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    validate_period(period)
    resolve_date_range(period, start_date, end_date)
    threshold = non_negative_number(z_threshold, "z_threshold")
    assert threshold is not None
    if threshold < 1.5 or threshold > 10:
        raise InputValidationError("z_threshold must be between 1.5 and 10")
    return _legacy("detect_anomalies")(
        db,
        period=period,
        category_id=category_id,
        z_threshold=threshold,
        start_date=start_date,
        end_date=end_date,
    )


def get_upcoming_payments(
    db: Any,
    days_ahead: int = 30,
) -> dict[str, Any]:
    days_ahead = bounded_int(days_ahead, "days_ahead", default=30, minimum=1, maximum=366)
    return _legacy("get_upcoming_payments")(db, days_ahead=days_ahead)
