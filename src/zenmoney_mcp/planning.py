"""Read-only financial planning analytics over the synchronized cache."""

from __future__ import annotations

import json
import statistics
import time
from datetime import date, datetime, timedelta
from typing import Any

from .financial_correctness import get_debts, get_liquidity, get_net_worth
from .money import CurrencyContext, convert, user_currency
from .periods import (
    Period,
    comparison_periods as resolve_comparison_periods,
    complete_months_with_activity,
    completed_periods,
    resolve_period,
)
from .validation import (
    InputValidationError,
    bounded_int,
    non_negative_number,
    parse_iso_date,
)


_OBLIGATION_CLASSES = {
    "loan": ("loan", "high"),
    "ccard": ("credit_card", "high"),
    "debt": ("personal_debt", "high"),
}
_VALID_OBLIGATION_CLASSES = {
    "loan",
    "credit_card",
    "installment",
    "personal_debt",
    "other",
}


def _data_quality(db: Any, as_of: date | None = None) -> dict[str, Any]:
    raw_sync = db.get_meta("last_sync_time")
    last_sync = None
    staleness = "never_synced"
    if raw_sync:
        try:
            stamp = int(raw_sync)
            last_sync = datetime.fromtimestamp(stamp).isoformat()
            age = int(time.time()) - stamp
            if age < 300:
                staleness = "fresh"
            elif age < 3600:
                staleness = "slightly_stale"
            else:
                staleness = "stale"
        except (TypeError, ValueError, OSError):
            staleness = "unknown"
    return {
        "last_sync": last_sync,
        "staleness": staleness,
        "complete_months_available": complete_months_with_activity(db, as_of),
        "missing_exchange_rates": [],
        "warnings": [],
    }


def _financial_obligations(
    db: Any,
    currency: CurrencyContext,
    overrides: dict[str, Any] | None = None,
    *,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict) or len(overrides) > 50:
        raise InputValidationError(
            "obligation_overrides must be an object with at most 50 accounts"
        )
    rows = _financial_obligation_accounts(db)
    obligations = []
    for row in rows:
        balance = convert(db, row["balance"], row["instrument"], currency)
        classification, confidence = _OBLIGATION_CLASSES.get(
            row["type"], ("other", "low")
        )
        obligations.append(
            {
                "account_id": row["id"],
                "title": row["title"],
                "classification": classification,
                "classification_confidence": confidence,
                "balance": round(-balance, 2),
                "currency": currency.code,
                "source_account_type": row["type"],
                "in_balance": bool(row["in_balance"]),
                "minimum_payment": {
                    "amount": None,
                    "due_date": None,
                    "source": "unknown",
                    "confidence": "low",
                },
                "apr_pct": {"value": None, "source": "unknown"},
            }
        )
    by_id = {item["account_id"]: item for item in obligations}
    marker_rows = db.connect().execute(
        """SELECT rm.date,rm.income_account AS obligation_id,
                  rm.outcome,
                  COALESCE(rm.outcome_instrument,source.instrument) AS instrument,
                  source.id AS source_id
           FROM reminder_markers rm
           JOIN accounts source ON source.id=rm.outcome_account
           WHERE rm.state='planned' AND rm.date>=?
             AND rm.income>0 AND rm.outcome>0
             AND COALESCE(source.archive,0)=0
             AND (source.in_balance IS NULL OR source.in_balance!=0)
           ORDER BY rm.date,rm.id""",
        ((as_of or date.today()).isoformat(),),
    ).fetchall()
    for marker in marker_rows:
        item = by_id.get(marker["obligation_id"])
        if item is None or marker["source_id"] in by_id:
            continue
        if item["minimum_payment"]["source"] != "unknown":
            continue
        item["minimum_payment"] = {
            "amount": round(
                convert(db, marker["outcome"], marker["instrument"], currency), 2
            ),
            "due_date": marker["date"],
            "source": "reminder",
            "confidence": "medium",
        }

    for account_id, override in overrides.items():
        path = f"obligation_overrides.{account_id}"
        item = by_id.get(account_id)
        if item is None:
            raise InputValidationError(f"{path} must identify an active obligation")
        if not isinstance(override, dict) or not override:
            raise InputValidationError(f"{path} must be a non-empty object")
        unknown = set(override) - {
            "classification",
            "minimum_payment",
            "apr_pct",
        }
        if unknown:
            raise InputValidationError(f"{path}.{sorted(unknown)[0]} is not supported")

        if "classification" in override:
            classification = override["classification"]
            if (
                not isinstance(classification, str)
                or classification not in _VALID_OBLIGATION_CLASSES
            ):
                raise InputValidationError(f"{path}.classification is invalid")
            item["classification"] = classification
            item["classification_confidence"] = "high"

        if "minimum_payment" in override:
            payment = override["minimum_payment"]
            payment_path = f"{path}.minimum_payment"
            if not isinstance(payment, dict):
                raise InputValidationError(f"{payment_path} must be an object")
            unknown_payment = set(payment) - {"amount", "due_date"}
            if unknown_payment:
                raise InputValidationError(
                    f"{payment_path}.{sorted(unknown_payment)[0]} is not supported"
                )
            if "amount" not in payment:
                raise InputValidationError(f"{payment_path}.amount is required")
            amount = non_negative_number(payment["amount"], f"{payment_path}.amount")
            due_date = payment.get("due_date")
            if due_date is not None:
                due_date = parse_iso_date(
                    due_date, f"{payment_path}.due_date"
                ).isoformat()
            item["minimum_payment"] = {
                "amount": round(amount, 2),
                "due_date": due_date,
                "source": "user_override",
                "confidence": "high",
            }

        if "apr_pct" in override:
            apr = non_negative_number(override["apr_pct"], f"{path}.apr_pct")
            item["apr_pct"] = {"value": apr, "source": "user_override"}
    return obligations


def _financial_obligation_accounts(db: Any) -> list[Any]:
    return db.connect().execute(
        """SELECT id,title,type,instrument,balance,in_balance
           FROM accounts
           WHERE COALESCE(archive,0)=0 AND balance<0
           ORDER BY title,id"""
    ).fetchall()


def _cash_flow_for_dates(
    db: Any,
    start: date,
    end: date,
    category_ids: set[str] | None = None,
) -> dict[str, Any]:
    currency = user_currency(db)
    obligation_ids = {row["id"] for row in _financial_obligation_accounts(db)}
    rows = db.connect().execute(
        """SELECT t.id,t.income,t.outcome,
                  t.income_instrument,t.outcome_instrument,
                  t.tag,tag.title AS category,
                  ia.id AS income_account_id,ia.type AS income_account_type,
                  ia.savings AS income_savings,
                  COALESCE(ia.archive,0) AS income_archive,ia.in_balance AS income_in_balance,
                  oa.id AS outcome_account_id,oa.type AS outcome_account_type,
                  oa.savings AS outcome_savings,
                  COALESCE(oa.archive,0) AS outcome_archive,oa.in_balance AS outcome_in_balance
           FROM transactions t
           LEFT JOIN accounts ia ON ia.id=t.income_account
           LEFT JOIN accounts oa ON oa.id=t.outcome_account
           LEFT JOIN tags tag ON tag.id=json_extract(t.tag,'$[0]')
           WHERE COALESCE(t.deleted,0)=0 AND COALESCE(t.hold,0)=0
             AND t.date BETWEEN ? AND ?
           ORDER BY t.date,t.id""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    components = {
        name: {"amount": 0.0, "count": 0}
        for name in (
            "income",
            "operating_expense",
            "internal_transfer",
            "financing_inflow",
            "debt_service_outflow",
            "asset_transfer",
            "unknown",
        )
    }
    categories: dict[str, dict[str, Any]] = {}
    uncertain: list[dict[str, Any]] = []

    def add_component(name: str, amount: float) -> None:
        components[name]["amount"] += amount
        components[name]["count"] += 1

    for row in rows:
        try:
            tag_ids = json.loads(row["tag"] or "[]")
        except (TypeError, json.JSONDecodeError):
            tag_ids = []
        category_id = (
            str(tag_ids[0]) if isinstance(tag_ids, list) and tag_ids else None
        )
        if category_ids is not None and category_id not in category_ids:
            continue

        income = float(row["income"] or 0)
        outcome = float(row["outcome"] or 0)
        income_obligation = row["income_account_id"] in obligation_ids
        outcome_obligation = row["outcome_account_id"] in obligation_ids
        income_asset = bool(
            row["income_account_id"]
            and not income_obligation
            and not row["income_archive"]
            and row["income_in_balance"] != 0
        )
        outcome_asset = bool(
            row["outcome_account_id"]
            and not outcome_obligation
            and not row["outcome_archive"]
            and row["outcome_in_balance"] != 0
        )
        income_relevant = income != 0 and (income_obligation or income_asset)
        outcome_relevant = outcome != 0 and (outcome_obligation or outcome_asset)
        if not (income_relevant or outcome_relevant):
            continue

        def add_category(side: str, amount: float) -> None:
            key = category_id or "uncategorized"
            item = categories.setdefault(
                key,
                {
                    "category_id": category_id,
                    "category": row["category"] or "Uncategorized",
                    "income": 0.0,
                    "outcome": 0.0,
                },
            )
            item[side] += amount

        if income > 0 and outcome == 0 and income_asset:
            amount = convert(db, income, row["income_instrument"], currency)
            add_component("income", amount)
            add_category("income", amount)
            continue

        if outcome > 0 and income == 0 and (outcome_asset or outcome_obligation):
            amount = convert(db, outcome, row["outcome_instrument"], currency)
            add_component("operating_expense", amount)
            add_category("outcome", amount)
            if outcome_obligation:
                add_component("financing_inflow", amount)
            continue

        if income > 0 and outcome > 0:
            source_amount = convert(
                db, outcome, row["outcome_instrument"], currency
            )
            if outcome_asset and income_obligation:
                add_component("debt_service_outflow", source_amount)
                continue
            if outcome_obligation and income_asset:
                add_component(
                    "financing_inflow",
                    convert(db, income, row["income_instrument"], currency),
                )
                continue
            if outcome_obligation and income_obligation:
                add_component("internal_transfer", source_amount)
                continue
            if outcome_asset and income_asset:
                is_asset_transfer = (
                    row["income_account_type"] == "deposit"
                    or row["outcome_account_type"] == "deposit"
                    or bool(row["income_savings"])
                    or bool(row["outcome_savings"])
                )
                add_component(
                    "asset_transfer" if is_asset_transfer else "internal_transfer",
                    source_amount,
                )
                continue

        unknown_amount = 0.0
        if outcome > 0 and (outcome_asset or outcome_obligation):
            unknown_amount = convert(
                db, outcome, row["outcome_instrument"], currency
            )
        elif income > 0 and (income_asset or income_obligation):
            unknown_amount = convert(db, income, row["income_instrument"], currency)
        add_component("unknown", unknown_amount)
        if len(uncertain) < 50:
            uncertain.append(
                {
                    "transaction_id": row["id"],
                    "classification": "unknown",
                    "classification_reason": "account_relationship_is_not_classifiable",
                    "confidence": "low",
                }
            )

    for item in categories.values():
        item["income"] = round(item["income"], 2)
        item["outcome"] = round(item["outcome"], 2)
    income = components["income"]["amount"]
    operating_expenses = components["operating_expense"]["amount"]
    financing_inflow = components["financing_inflow"]["amount"]
    debt_service = components["debt_service_outflow"]["amount"]
    for component in components.values():
        component["amount"] = round(component["amount"], 2)
    return {
        "currency": currency.code,
        "income": round(income, 2),
        "operating_expenses": round(operating_expenses, 2),
        "operating_net_cash_flow": round(income - operating_expenses, 2),
        "financing_inflow": round(financing_inflow, 2),
        "debt_service_cash_outflow": round(debt_service, 2),
        "net_cash_flow_after_debt_service": round(
            income - operating_expenses + financing_inflow - debt_service, 2
        ),
        "flow_components": components,
        "uncertain_transactions": uncertain,
        "categories": categories,
    }


def get_cash_flow(
    db: Any,
    period: str = "current_period",
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    selected = resolve_period(db, period, start_date, end_date, as_of)
    totals = _cash_flow_for_dates(db, selected.start, selected.end)
    income = totals["income"]
    operating_net = totals["operating_net_cash_flow"]
    net_after_debt = totals["net_cash_flow_after_debt_service"]
    data_quality = _data_quality(db, as_of)
    if totals["flow_components"]["unknown"]["count"]:
        data_quality["warnings"].append("unknown_transaction_flows_excluded")
    return {
        "period": {
            "preset": selected.label,
            "start": selected.start.isoformat(),
            "end": selected.end.isoformat(),
            "complete": selected.complete,
        },
        "currency": totals["currency"],
        "income": income,
        "operating_expenses": totals["operating_expenses"],
        "operating_net_cash_flow": operating_net,
        "financing_inflow": totals["financing_inflow"],
        "debt_service_cash_outflow": totals["debt_service_cash_outflow"],
        "net_cash_flow_after_debt_service": net_after_debt,
        "savings_rate_before_debt_service_pct": (
            round(operating_net / income * 100, 2) if income > 0 else None
        ),
        "savings_rate_after_debt_service_pct": (
            round(net_after_debt / income * 100, 2) if income > 0 else None
        ),
        "flow_components": totals["flow_components"],
        "uncertain_transactions": totals["uncertain_transactions"],
        "data_quality": data_quality,
    }


def _descendant_category_ids(db: Any, category_id: str) -> set[str]:
    rows = db.connect().execute(
        """WITH RECURSIVE descendants(id) AS (
               SELECT ?
               UNION ALL
               SELECT t.id FROM tags t JOIN descendants d ON t.parent=d.id
           ) SELECT id FROM descendants""",
        (category_id,),
    ).fetchall()
    return {str(row["id"]) for row in rows}


def get_spending_baseline(
    db: Any,
    months: int = 6,
    category_id: str | None = None,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    months = bounded_int(months, "months", default=6, minimum=3, maximum=24)
    selected = completed_periods(db, months, as_of)
    category_ids = _descendant_category_ids(db, category_id) if category_id else None
    monthly = []
    values = []
    for period in selected:
        outcome = _cash_flow_for_dates(
            db, period.start, period.end, category_ids
        )["operating_expenses"]
        values.append(outcome)
        monthly.append(
            {
                "label": period.label,
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
                "outcome": outcome,
            }
        )
    baseline = statistics.median(values)
    p25, _, p75 = statistics.quantiles(values, n=4, method="inclusive")
    latest = values[-1]
    return {
        "currency": user_currency(db).code,
        "category_id": category_id,
        "months_used": months,
        "monthly": monthly,
        "mean": round(statistics.fmean(values), 2),
        "median": round(baseline, 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "p25": round(p25, 2),
        "p75": round(p75, 2),
        "latest_complete_month": round(latest, 2),
        "latest_vs_median_pct": (
            round((latest - baseline) / baseline * 100, 2) if baseline else None
        ),
        "percentile_method": "statistics.quantiles inclusive",
        "data_quality": _data_quality(db, as_of),
    }


def _custom_comparison_period(
    value: dict[str, str] | None, name: str, as_of: date
) -> Period:
    if not isinstance(value, dict):
        raise InputValidationError(f"{name} is required for a custom comparison")
    try:
        start_raw, end_raw = value["start_date"], value["end_date"]
        start, end = date.fromisoformat(start_raw), date.fromisoformat(end_raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise InputValidationError(
            f"{name} must contain YYYY-MM-DD start_date and end_date"
        ) from exc
    if end < start:
        raise InputValidationError(f"{name}.end_date must not precede start_date")
    return Period(start, end, name, end < as_of)


def _delta(period_a: float, period_b: float) -> dict[str, float | None]:
    change = period_b - period_a
    return {
        "period_a": round(period_a, 2),
        "period_b": round(period_b, 2),
        "delta": round(change, 2),
        "delta_pct": round(change / period_a * 100, 2) if period_a else None,
    }


def _period_payload(period: Period) -> dict[str, str | bool]:
    return {
        "start": period.start.isoformat(),
        "end": period.end.isoformat(),
        "complete": period.complete,
    }


def compare_periods(
    db: Any,
    preset: str = "last_month_vs_previous",
    period_a: dict[str, str] | None = None,
    period_b: dict[str, str] | None = None,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    today = as_of or date.today()
    if period_a is not None or period_b is not None:
        first = _custom_comparison_period(period_a, "period_a", today)
        second = _custom_comparison_period(period_b, "period_b", today)
        label = "custom"
    else:
        first, second = resolve_comparison_periods(db, preset, as_of)
        label = preset
    a = _cash_flow_for_dates(db, first.start, first.end)
    b = _cash_flow_for_dates(db, second.start, second.end)
    category_deltas = []
    for key in sorted(set(a["categories"]) | set(b["categories"])):
        left = a["categories"].get(key, {})
        right = b["categories"].get(key, {})
        left_outcome = float(left.get("outcome", 0))
        right_outcome = float(right.get("outcome", 0))
        if not left_outcome and not right_outcome:
            continue
        category_deltas.append(
            {
                "category_id": left.get("category_id", right.get("category_id")),
                "category": left.get("category", right.get("category")),
                **_delta(left_outcome, right_outcome),
            }
        )
    return {
        "comparison": label,
        "currency": a["currency"],
        "period_a": _period_payload(first),
        "period_b": _period_payload(second),
        "income": _delta(a["income"], b["income"]),
        "outcome": _delta(a["operating_expenses"], b["operating_expenses"]),
        "net_cash_flow": _delta(
            a["operating_net_cash_flow"], b["operating_net_cash_flow"]
        ),
        "category_deltas": category_deltas,
        "data_quality": _data_quality(db, as_of),
    }


def get_emergency_fund_status(
    db: Any,
    essential_category_ids: list[str] | None = None,
    monthly_essential_override: float | None = None,
    baseline_months: int = 6,
    target_months: int = 6,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    if not essential_category_ids and monthly_essential_override is None:
        return {
            "status": "configuration_required",
            "missing": ["essential_category_ids or monthly_essential_override"],
            "data_quality": _data_quality(db, as_of),
        }
    if essential_category_ids and monthly_essential_override is not None:
        raise InputValidationError(
            "essential_category_ids and monthly_essential_override are mutually exclusive"
        )
    target_months = bounded_int(
        target_months, "target_months", default=6, minimum=1, maximum=60
    )
    baseline_months = bounded_int(
        baseline_months, "baseline_months", default=6, minimum=3, maximum=24
    )
    if monthly_essential_override is not None:
        monthly = non_negative_number(
            monthly_essential_override, "monthly_essential_override"
        )
        assert monthly is not None
        source = "override"
    else:
        if not isinstance(essential_category_ids, list) or not all(
            isinstance(item, str) and item for item in essential_category_ids
        ):
            raise InputValidationError("essential_category_ids must contain category IDs")
        category_ids: set[str] = set()
        for item in essential_category_ids:
            category_ids.update(_descendant_category_ids(db, item))
        values = [
            _cash_flow_for_dates(db, period.start, period.end, category_ids)[
                "operating_expenses"
            ]
            for period in completed_periods(db, baseline_months, as_of)
        ]
        monthly = float(statistics.median(values))
        source = "essential_categories"
    liquidity = get_liquidity(db)
    own = float(liquidity["liquid_own"])
    savings = float(liquidity["savings_accessible"])
    reserve = own + savings
    target = monthly * target_months
    gap = target - reserve
    if monthly == 0:
        coverage = None
        status = "configuration_required"
    elif abs(gap) < 0.005:
        coverage = reserve / monthly
        status = "at_target"
    elif gap > 0:
        coverage = reserve / monthly
        status = "below_target"
    else:
        coverage = reserve / monthly
        status = "above_target"
    return {
        "currency": liquidity["currency"],
        "baseline_source": source,
        "monthly_essential_baseline": round(monthly, 2),
        "reserve": {
            "own_liquid_funds": round(own, 2),
            "accessible_savings": round(savings, 2),
            "total_eligible": round(reserve, 2),
        },
        "coverage_months": round(coverage, 2) if coverage is not None else None,
        "target_months": target_months,
        "target_amount": round(target, 2),
        "gap": round(gap, 2),
        "status": status,
        "data_quality": _data_quality(db, as_of),
    }


def get_debt_service(
    db: Any,
    obligation_overrides: dict[str, Any] | None = None,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    currency = user_currency(db)
    obligations = _financial_obligations(
        db, currency, obligation_overrides, as_of=as_of
    )
    periods = completed_periods(db, 3, as_of)
    flows = [
        _cash_flow_for_dates(db, period.start, period.end)
        for period in periods
    ]
    latest = flows[-1]
    income = latest["income"]
    latest_payment = latest["debt_service_cash_outflow"]
    payments = [item["debt_service_cash_outflow"] for item in flows]
    data_quality = _data_quality(db, as_of)
    if any(item["flow_components"]["unknown"]["count"] for item in flows):
        data_quality["warnings"].append("unknown_transaction_flows_excluded")
    return {
        "currency": currency.code,
        "total_liabilities": round(
            sum(item["balance"] for item in obligations), 2
        ),
        "obligations": obligations,
        "last_complete_month": {
            "operating_income": round(income, 2),
            "debt_service_cash_outflow": round(latest_payment, 2),
            "debt_service_ratio_pct": (
                round(latest_payment / income * 100, 2) if income > 0 else None
            ),
        },
        "trailing_3_complete_months": {
            "average_debt_service_cash_outflow": round(
                statistics.fmean(payments), 2
            )
        },
        "data_quality": data_quality,
    }


def _scheduled_flows(
    db: Any, horizon_days: int, as_of: date
) -> tuple[dict[str, float], set[str], int]:
    currency = user_currency(db)
    rows = db.connect().execute(
        """SELECT rm.income,rm.outcome,rm.income_instrument,rm.outcome_instrument,
                  rm.payee,m.title AS merchant,
                  ia.instrument AS income_account_instrument,
                  oa.instrument AS outcome_account_instrument,
                  COALESCE(ia.archive,0) AS income_archive,ia.in_balance AS income_in_balance,
                  COALESCE(oa.archive,0) AS outcome_archive,oa.in_balance AS outcome_in_balance
           FROM reminder_markers rm
           LEFT JOIN accounts ia ON ia.id=rm.income_account
           LEFT JOIN accounts oa ON oa.id=rm.outcome_account
           LEFT JOIN merchants m ON m.id=rm.merchant
           WHERE rm.state='planned' AND rm.date BETWEEN ? AND ?
           ORDER BY rm.date,rm.id""",
        (
            as_of.isoformat(),
            (as_of + timedelta(days=horizon_days - 1)).isoformat(),
        ),
    ).fetchall()
    income = outcome = 0.0
    names: set[str] = set()
    count = 0
    for row in rows:
        incoming, outgoing = float(row["income"] or 0), float(row["outcome"] or 0)
        if incoming > 0 and outgoing == 0:
            if row["income_archive"] or row["income_in_balance"] == 0:
                continue
            instrument = row["income_instrument"] or row["income_account_instrument"]
            income += convert(db, incoming, instrument, currency)
        elif outgoing > 0 and incoming == 0:
            if row["outcome_archive"] or row["outcome_in_balance"] == 0:
                continue
            instrument = row["outcome_instrument"] or row["outcome_account_instrument"]
            outcome += convert(db, outgoing, instrument, currency)
        else:
            continue
        count += 1
        name = _normalize_name(row["merchant"] or row["payee"])
        if name:
            names.add(name)
    return (
        {
            "income": round(income, 2),
            "outcome": round(outcome, 2),
            "net": round(income - outcome, 2),
        },
        names,
        count,
    )


def _normalize_name(value: str | None) -> str:
    return "".join(
        character
        for character in (value or "").casefold()
        if character.isalnum()
    )


def _detected_recurring(db: Any, as_of: date) -> list[dict[str, Any]]:
    currency = user_currency(db)
    periods = completed_periods(db, 6, as_of)
    rows = db.connect().execute(
        """SELECT t.date,t.outcome,t.outcome_instrument,t.payee,m.title AS merchant
           FROM transactions t
           LEFT JOIN accounts a ON a.id=t.outcome_account
           LEFT JOIN merchants m ON m.id=t.merchant
           WHERE COALESCE(t.deleted,0)=0 AND COALESCE(t.hold,0)=0
             AND t.outcome>0 AND t.income=0 AND t.reminder_marker IS NULL
             AND t.date BETWEEN ? AND ?
             AND COALESCE(a.archive,0)=0 AND (a.in_balance=1 OR a.in_balance IS NULL)
           ORDER BY t.date,t.id""",
        (periods[0].start.isoformat(), periods[-1].end.isoformat()),
    ).fetchall()
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row["merchant"] or row["payee"]
        key = _normalize_name(name)
        if not key:
            continue
        group = groups.setdefault(key, {"name": name, "dates": [], "amounts": []})
        group["dates"].append(date.fromisoformat(row["date"]))
        group["amounts"].append(
            convert(db, row["outcome"], row["outcome_instrument"], currency)
        )
    recurring = []
    for key, group in groups.items():
        if len(group["dates"]) < 3:
            continue
        intervals = [
            (right - left).days
            for left, right in zip(group["dates"], group["dates"][1:])
        ]
        average_interval = statistics.fmean(intervals)
        if 25 <= average_interval <= 35:
            multiplier, frequency = 1.0, "monthly"
        elif 6 <= average_interval <= 8:
            multiplier, frequency = 52 / 12, "weekly"
        elif 12 <= average_interval <= 16:
            multiplier, frequency = 26 / 12, "biweekly"
        elif 85 <= average_interval <= 95:
            multiplier, frequency = 1 / 3, "quarterly"
        else:
            continue
        average_amount = statistics.fmean(group["amounts"])
        if average_amount <= 0 or (
            max(group["amounts"]) - min(group["amounts"])
        ) / average_amount > 0.10:
            continue
        recurring.append(
            {
                "name": group["name"],
                "normalized_name": key,
                "frequency": frequency,
                "monthly_estimate": round(average_amount * multiplier, 2),
                "confidence": "medium",
            }
        )
    return recurring


def _scenario(starting: float, income: float, outcome: float) -> dict[str, float]:
    return {
        "income": round(income, 2),
        "outcome": round(outcome, 2),
        "net": round(income - outcome, 2),
        "ending_liquid_funds": round(starting + income - outcome, 2),
    }


def forecast_cash_flow(
    db: Any, horizon_days: int = 90, *, as_of: date | None = None
) -> dict[str, Any]:
    if isinstance(horizon_days, bool) or horizon_days not in (30, 60, 90):
        raise InputValidationError("horizon_days must be one of 30, 60, or 90")
    today = as_of or date.today()
    currency = user_currency(db)
    liquidity = get_liquidity(db)
    starting = float(liquidity["total_available"])
    scheduled, scheduled_names, scheduled_count = _scheduled_flows(
        db, horizon_days, today
    )
    recurring = _detected_recurring(db, today)
    unmatched_monthly = sum(
        item["monthly_estimate"]
        for item in recurring
        if item["normalized_name"] not in scheduled_names
    )
    recurring_outcome = unmatched_monthly * horizon_days / 30
    baseline = get_spending_baseline(db, months=6, as_of=today)["median"]
    baseline_outcome = baseline * horizon_days / 30
    warnings = []
    if scheduled_count == 0:
        warnings.append("no planned reminder markers in horizon")
    if not recurring:
        warnings.append("no recurring outcomes detected from completed history")
    return {
        "horizon_days": horizon_days,
        "currency": currency.code,
        "starting_liquid_funds": round(starting, 2),
        "scheduled": scheduled,
        "detected_recurring": {
            "outcome": round(recurring_outcome, 2),
            "confidence": "medium",
        },
        "baseline_discretionary_spend": {"monthly_median": round(baseline, 2)},
        "scenarios": {
            "scheduled_only": _scenario(
                starting, scheduled["income"], scheduled["outcome"]
            ),
            "scheduled_plus_recurring": _scenario(
                starting,
                scheduled["income"],
                scheduled["outcome"] + recurring_outcome,
            ),
            "baseline_spending": _scenario(
                starting,
                scheduled["income"],
                max(scheduled["outcome"], baseline_outcome),
            ),
        },
        "assumptions": [
            "planned reminder markers are high-confidence scheduled flows",
            "detected recurring outcomes are medium-confidence historical heuristics",
            "scheduled payees replace matching detected recurring payees",
            "spending baseline is total household spending because ZenMoney does not identify discretionary expenses",
            "baseline spending uses the greater of scheduled outcome and prorated completed-month median",
        ],
        "warnings": warnings,
        "data_quality": _data_quality(db, today),
    }


def _average_cash_flow(db: Any, count: int, as_of: date) -> dict[str, float]:
    values = [
        _cash_flow_for_dates(db, period.start, period.end)
        for period in completed_periods(db, count, as_of)
    ]
    income = statistics.fmean(item["income"] for item in values)
    outcome = statistics.fmean(item["operating_expenses"] for item in values)
    return {
        "income": round(income, 2),
        "outcome": round(outcome, 2),
        "net_cash_flow": round(income - outcome, 2),
    }


def get_financial_snapshot(db: Any, *, as_of: date | None = None) -> dict[str, Any]:
    today = as_of or date.today()
    net_worth = get_net_worth(db)
    liquidity = get_liquidity(db)
    debts = get_debts(db)
    last = get_cash_flow(db, "last_complete_month", as_of=today)
    scheduled, _, _ = _scheduled_flows(db, 30, today)
    recurring_monthly = sum(
        item["monthly_estimate"] for item in _detected_recurring(db, today)
    )
    debt_summary = debts["summary"]
    return {
        "as_of": today.isoformat(),
        "currency": net_worth["currency"],
        "net_worth": net_worth["net_worth"],
        "own_liquid_funds": liquidity["liquid_own"],
        "accessible_savings": liquidity["savings_accessible"],
        "restricted_savings": liquidity["restricted_savings"],
        "debt_position": {
            "owed_to_you": debt_summary["total_owed_to_you"],
            "you_owe": debt_summary["total_you_owe"],
            "net": debt_summary["net_position"],
        },
        "cash_flow": {
            "last_complete_month": {
                "income": last["income"],
                "outcome": last["operating_expenses"],
                "net_cash_flow": last["operating_net_cash_flow"],
            },
            "trailing_3_months_average": _average_cash_flow(db, 3, today),
            "trailing_12_months_average": _average_cash_flow(db, 12, today),
        },
        "recurring_obligations_monthly_estimate": round(recurring_monthly, 2),
        "upcoming_30_days": {
            "planned_income": scheduled["income"],
            "planned_outcome": scheduled["outcome"],
            "planned_net": scheduled["net"],
        },
        "data_quality": _data_quality(db, today),
    }
