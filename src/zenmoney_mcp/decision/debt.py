"""Deterministic debt amortization strategies."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from ..planning import get_debt_service
from ..validation import InputValidationError
from .models import HUNDRED, decimal_number, money, month_end_after, number

STRATEGIES = {"minimum_only", "avalanche", "snowball", "custom"}
MAX_PAYOFF_MONTHS = 120
LIABILITY_TYPES = {"fixed_loan", "credit_card", "installment", "arbitrary"}
_INFERRED_LIABILITY_TYPE = {
    "loan": "fixed_loan",
    "credit_card": "credit_card",
    "installment": "installment",
    "personal_debt": "arbitrary",
    "other": "arbitrary",
}
_CONFIG_FIELDS = {
    "liability_type",
    "title",
    "balance",
    "apr_pct",
    "fixed_payment",
    "minimum_payment",
    "statement_balance",
    "grace_period_payment",
    "grace_period_due_date",
    "payment_schedule",
}
_TYPE_FIELDS = {
    "fixed_loan": {"apr_pct", "fixed_payment", "minimum_payment"},
    "credit_card": {
        "apr_pct",
        "minimum_payment",
        "statement_balance",
        "grace_period_payment",
        "grace_period_due_date",
    },
    "installment": {"apr_pct", "payment_schedule"},
    "arbitrary": {"apr_pct", "minimum_payment"},
}
_COMMON_FIELDS = {"liability_type", "title", "balance"}


def _missing(field: str, reason: str) -> dict[str, str]:
    return {"field": field, "reason": reason}


def _future_date(value: Any, field: str, as_of: date) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InputValidationError(f"{field} must be a real ISO date") from exc
    if parsed.isoformat() != value or parsed <= as_of:
        raise InputValidationError(f"{field} must be after as_of")
    return parsed


def _month_index(value: date, as_of: date) -> int:
    return (value.year - as_of.year) * 12 + value.month - as_of.month


def _configured_states(
    obligations: list[dict[str, Any]],
    configured: dict[str, Any],
    strategy: str,
    as_of: date,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    accounts = {
        item["account_id"]: {
            "id": item["account_id"],
            "title": item["title"],
            "debt_balance": item["balance"],
            "classification": item["classification"],
            "from_snapshot": True,
        }
        for item in obligations
    }
    for account_id, values in configured.items():
        if account_id in accounts:
            continue
        if not isinstance(values, dict):
            raise InputValidationError(f"debt_accounts.{account_id} must be an object")
        if values.get("liability_type") != "arbitrary" or "balance" not in values:
            raise InputValidationError(
                f"debt_accounts.{account_id} must identify an active obligation or define an arbitrary balance"
            )
        title = values.get("title", account_id)
        if not isinstance(title, str) or not title:
            raise InputValidationError(f"debt_accounts.{account_id}.title must be a non-empty string")
        accounts[account_id] = {
            "id": account_id,
            "title": title,
            "debt_balance": values["balance"],
            "classification": "other",
            "from_snapshot": False,
        }

    states: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for account in accounts.values():
        account_id = account["id"]
        path = f"debt_accounts.{account_id}"
        values = configured.get(account_id)
        if not isinstance(values, dict):
            missing.append(_missing(path, "Required for an active liability"))
            continue
        unknown = set(values) - _CONFIG_FIELDS
        if unknown:
            raise InputValidationError(f"{path}.{sorted(unknown)[0]} is not supported")
        title = values.get("title", account["title"])
        if not isinstance(title, str) or not title:
            raise InputValidationError(f"{path}.title must be a non-empty string")
        liability_type = values.get(
            "liability_type",
            _INFERRED_LIABILITY_TYPE[account["classification"]],
        )
        if liability_type not in LIABILITY_TYPES:
            raise InputValidationError(
                f"{path}.liability_type must be one of {sorted(LIABILITY_TYPES)}"
            )
        incompatible = set(values) - _COMMON_FIELDS - _TYPE_FIELDS[liability_type]
        if incompatible:
            raise InputValidationError(
                f"{path}.{sorted(incompatible)[0]} is not supported for {liability_type}"
            )
        balance = money(
            decimal_number(
                values.get("balance", account["debt_balance"]),
                f"{path}.balance",
                minimum=Decimal(0),
            )
        )
        if balance <= 0:
            raise InputValidationError(f"{path}.balance must be positive")

        apr = Decimal(0)
        minimum = Decimal(0)
        schedule: dict[int, Decimal] = {}
        statement_balance: Decimal | None = None
        grace_payment: Decimal | None = None
        grace_due_date: date | None = None

        if liability_type == "fixed_loan":
            if "apr_pct" not in values:
                missing.append(
                    _missing(
                        f"{path}.apr_pct",
                        "Required to calculate interest and avalanche priority"
                        if strategy == "avalanche"
                        else "Required to calculate interest",
                    )
                )
            else:
                apr = decimal_number(
                    values["apr_pct"], f"{path}.apr_pct", minimum=Decimal(0)
                )
            if "fixed_payment" in values and "minimum_payment" in values:
                raise InputValidationError(
                    f"{path}.fixed_payment and minimum_payment are mutually exclusive"
                )
            payment_field = (
                "fixed_payment" if "fixed_payment" in values else "minimum_payment"
            )
            if payment_field not in values:
                missing.append(
                    _missing(
                        f"{path}.minimum_payment",
                        "Required to calculate the monthly debt budget",
                    )
                )
            else:
                minimum = money(
                    decimal_number(
                        values[payment_field],
                        f"{path}.{payment_field}",
                        minimum=Decimal(0),
                    )
                )
        elif liability_type == "credit_card":
            for field, reason in (
                ("apr_pct", "Required to calculate credit-card interest"),
                ("minimum_payment", "Required to calculate the monthly debt budget"),
            ):
                if field not in values:
                    missing.append(_missing(f"{path}.{field}", reason))
            if "apr_pct" in values:
                apr = decimal_number(
                    values["apr_pct"], f"{path}.apr_pct", minimum=Decimal(0)
                )
            if "minimum_payment" in values:
                minimum = money(
                    decimal_number(
                        values["minimum_payment"],
                        f"{path}.minimum_payment",
                        minimum=Decimal(0),
                    )
                )
            if "statement_balance" in values:
                statement_balance = money(
                    decimal_number(
                        values["statement_balance"],
                        f"{path}.statement_balance",
                        minimum=Decimal(0),
                    )
                )
                if statement_balance > balance:
                    raise InputValidationError(
                        f"{path}.statement_balance must not exceed balance"
                    )
            grace_fields = {
                "grace_period_payment",
                "grace_period_due_date",
            } & set(values)
            if grace_fields:
                if grace_fields != {"grace_period_payment", "grace_period_due_date"}:
                    raise InputValidationError(
                        f"{path}.grace_period_payment and grace_period_due_date must be provided together"
                    )
                grace_payment = money(
                    decimal_number(
                        values["grace_period_payment"],
                        f"{path}.grace_period_payment",
                        minimum=Decimal(0),
                    )
                )
                grace_due_date = _future_date(
                    values["grace_period_due_date"],
                    f"{path}.grace_period_due_date",
                    as_of,
                )
                if statement_balance is not None and grace_payment > statement_balance:
                    raise InputValidationError(
                        f"{path}.grace_period_payment must not exceed statement_balance"
                    )
        elif liability_type == "installment":
            raw_schedule = values.get("payment_schedule")
            if not isinstance(raw_schedule, list) or not 1 <= len(raw_schedule) <= 120:
                raise InputValidationError(
                    f"{path}.payment_schedule must contain 1 to 120 payments"
                )
            seen_dates: set[date] = set()
            for position, payment in enumerate(raw_schedule):
                payment_path = f"{path}.payment_schedule.{position}"
                if not isinstance(payment, dict) or set(payment) != {"date", "amount"}:
                    raise InputValidationError(
                        f"{payment_path} must contain date and amount"
                    )
                payment_date = _future_date(
                    payment["date"], f"{payment_path}.date", as_of
                )
                if payment_date in seen_dates:
                    raise InputValidationError(
                        f"{path}.payment_schedule dates must be unique"
                    )
                seen_dates.add(payment_date)
                amount = money(
                    decimal_number(
                        payment["amount"],
                        f"{payment_path}.amount",
                        minimum=Decimal(0),
                    )
                )
                if amount <= 0:
                    raise InputValidationError(f"{payment_path}.amount must be positive")
                month = _month_index(payment_date, as_of)
                if month > MAX_PAYOFF_MONTHS:
                    raise InputValidationError(
                        f"{payment_path}.date exceeds the planning horizon"
                    )
                schedule[month] = schedule.get(month, Decimal(0)) + amount
            if "apr_pct" in values:
                apr = decimal_number(
                    values["apr_pct"], f"{path}.apr_pct", minimum=Decimal(0)
                )
        else:
            if "minimum_payment" not in values:
                if not account["from_snapshot"]:
                    raise InputValidationError(f"{path}.minimum_payment is required")
                missing.append(
                    _missing(
                        f"{path}.minimum_payment",
                        "Required to calculate the monthly debt budget",
                    )
                )
            else:
                minimum = money(
                    decimal_number(
                        values["minimum_payment"],
                        f"{path}.minimum_payment",
                        minimum=Decimal(0),
                    )
                )
            if "apr_pct" in values:
                apr = decimal_number(
                    values["apr_pct"], f"{path}.apr_pct", minimum=Decimal(0)
                )

        states.append(
            {
                "id": account_id,
                "title": title,
                "liability_type": liability_type,
                "balance": balance,
                "starting_balance": balance,
                "apr": apr,
                "minimum": minimum,
                "schedule": schedule,
                "statement_balance": statement_balance,
                "grace_payment": grace_payment,
                "grace_due_date": grace_due_date,
                "grace_month": (
                    _month_index(grace_due_date, as_of)
                    if grace_due_date is not None
                    else None
                ),
                "interest": Decimal(0),
                "payoff_month": None,
            }
        )
    return states, missing


def _planned_payment(state: dict[str, Any], month: int) -> Decimal:
    if state["liability_type"] == "installment":
        return state["schedule"].get(month, Decimal(0))
    if month == 0 and state["grace_month"] != 0:
        return Decimal(0)
    payment = state["minimum"]
    if state["grace_month"] == month and state["grace_payment"] is not None:
        payment = max(payment, state["grace_payment"])
    return payment


def _has_future_event(states: list[dict[str, Any]], month: int) -> bool:
    return any(
        any(payment_month > month for payment_month in state["schedule"])
        or (state["grace_month"] is not None and state["grace_month"] > month)
        for state in states
        if state["balance"] > 0
    )


def _priority(strategy: str, states: list[dict[str, Any]], custom_order: list[str] | None):
    active = [state for state in states if state["balance"] > 0]
    if strategy == "avalanche":
        return sorted(active, key=lambda item: (-item["apr"], item["balance"], item["id"]))
    if strategy == "snowball":
        return sorted(active, key=lambda item: (item["balance"], -item["apr"], item["id"]))
    positions = {account_id: index for index, account_id in enumerate(custom_order or [])}
    return sorted(active, key=lambda item: positions[item["id"]])


def plan_debt_payoff(
    db: Any,
    monthly_extra_payment: float = 0,
    strategy: str = "avalanche",
    debt_accounts: dict[str, Any] | None = None,
    custom_order: list[str] | None = None,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Amortize active debts using minimum, avalanche, snowball, or explicit order."""
    if strategy not in STRATEGIES:
        raise InputValidationError(f"strategy must be one of {sorted(STRATEGIES)}")
    extra = decimal_number(monthly_extra_payment, "monthly_extra_payment", minimum=Decimal(0))
    configured = {} if debt_accounts is None else debt_accounts
    if not isinstance(configured, dict) or len(configured) > 50:
        raise InputValidationError("debt_accounts must be an object with at most 50 accounts")

    today = as_of or date.today()
    facts = get_debt_service(db, as_of=today)
    if len(facts["obligations"]) > 50:
        raise InputValidationError("at most 50 active debt accounts are supported")
    states, missing = _configured_states(
        facts["obligations"], configured, strategy, today
    )
    if len(states) > 50:
        raise InputValidationError("at most 50 active liabilities are supported")
    if missing:
        return {"status": "configuration_required", "missing": missing}
    if not states:
        return {
            "strategy": strategy,
            "currency": facts["currency"],
            "starting_debt": 0.0,
            "monthly_budget": {
                "minimum_payments": 0.0,
                "extra_payment": 0.0,
                "total": 0.0,
            },
            "estimated_payoff_months": 0,
            "estimated_interest": 0.0,
            "accounts": [],
            "schedule": [],
            "warnings": [],
            "assumptions": ["interest is applied before payment at each calendar month end"],
            "data_quality": "high",
            "limitations": [],
        }

    if strategy == "custom":
        active_ids = {state["id"] for state in states}
        if not isinstance(custom_order, list) or set(custom_order) != active_ids or len(custom_order) != len(active_ids):
            return {
                "status": "configuration_required",
                "missing": [
                    _missing(
                        "custom_order",
                        "Must list every active liability exactly once",
                    )
                ],
            }

    first_calendar_month = (
        0
        if any(
            state["grace_month"] == 0 or 0 in state["schedule"]
            for state in states
        )
        else 1
    )
    monthly_minimum_budget = sum(
        (_planned_payment(state, 1) for state in states),
        Decimal(0),
    )
    applied_extra = Decimal(0) if strategy == "minimum_only" else money(extra)
    starting_debt = sum((state["balance"] for state in states), Decimal(0))
    schedule = []
    warnings: list[str] = []

    period_count = MAX_PAYOFF_MONTHS + (first_calendar_month == 0)
    for month in range(1, period_count + 1):
        calendar_month = first_calendar_month + month - 1
        planned_budget = sum(
            (_planned_payment(state, calendar_month) for state in states),
            Decimal(0),
        )
        period_extra = Decimal(0) if calendar_month == 0 else applied_extra
        total_budget = planned_budget + period_extra
        rows: dict[str, dict[str, Decimal | str]] = {}
        for state in states:
            opening = state["balance"]
            interest = (
                Decimal(0)
                if calendar_month == 0
                else money(opening * state["apr"] / HUNDRED / 12)
            )
            due = opening + interest
            payment = min(_planned_payment(state, calendar_month), due)
            state["balance"] = money(due - payment)
            state["interest"] += interest
            rows[state["id"]] = {
                "account_id": state["id"],
                "opening_balance": opening,
                "interest": interest,
                "payment": payment,
            }

        if strategy != "minimum_only" and calendar_month != 0:
            remaining = money(total_budget - sum((row["payment"] for row in rows.values()), Decimal(0)))
            for state in _priority(strategy, states, custom_order):
                if remaining <= 0:
                    break
                addition = min(remaining, state["balance"])
                state["balance"] = money(state["balance"] - addition)
                rows[state["id"]]["payment"] += addition
                remaining -= addition

        no_positive_principal = True
        month_rows = []
        for state in sorted(states, key=lambda item: item["id"]):
            row = rows[state["id"]]
            principal = money(row["payment"] - row["interest"])
            if principal > 0:
                no_positive_principal = False
            if state["balance"] == 0 and state["payoff_month"] is None:
                state["payoff_month"] = month
            if state["balance"] > 0 and row["payment"] <= row["interest"]:
                if "negative_amortization" not in warnings:
                    warnings.append("negative_amortization")
            month_rows.append(
                {
                    "account_id": state["id"],
                    "opening_balance": number(row["opening_balance"]),
                    "interest": number(row["interest"]),
                    "payment": number(row["payment"]),
                    "principal": number(principal),
                    "ending_balance": number(state["balance"]),
                }
            )
        ending_debt = sum((state["balance"] for state in states), Decimal(0))
        schedule.append(
            {
                "month": month,
                "date": month_end_after(today, calendar_month).isoformat(),
                "accounts": month_rows,
                "planned_budget": number(total_budget),
                "total_payment": number(sum((Decimal(str(row["payment"])) for row in month_rows), Decimal(0))),
                "total_interest": number(sum((Decimal(str(row["interest"])) for row in month_rows), Decimal(0))),
                "ending_debt": number(ending_debt),
            }
        )
        if ending_debt == 0:
            payoff_months: int | None = month
            break
        if no_positive_principal and not _has_future_event(states, calendar_month):
            payoff_months = None
            if any(
                state["liability_type"] == "installment" and state["balance"] > 0
                for state in states
            ):
                warnings.append("insufficient_payment_schedule")
            break
    else:
        payoff_months = None
        warnings.append("payoff_horizon_exceeded")

    return {
        "strategy": strategy,
        "currency": facts["currency"],
        "starting_debt": number(starting_debt),
        "monthly_budget": {
            "minimum_payments": number(monthly_minimum_budget),
            "extra_payment": number(applied_extra),
            "total": number(monthly_minimum_budget + applied_extra),
            "variable": any(state["schedule"] for state in states)
            or any(state["grace_payment"] is not None for state in states),
        },
        "estimated_payoff_months": payoff_months,
        "estimated_interest": number(sum((state["interest"] for state in states), Decimal(0))),
        "accounts": [
            {
                "account_id": state["id"],
                "title": state["title"],
                "liability_type": state["liability_type"],
                "starting_balance": number(state["starting_balance"]),
                "apr_pct": number(state["apr"]),
                "minimum_payment": number(state["minimum"]),
                "statement_balance": (
                    number(state["statement_balance"])
                    if state["statement_balance"] is not None
                    else None
                ),
                "grace_period_payment": (
                    number(state["grace_payment"])
                    if state["grace_payment"] is not None
                    else None
                ),
                "grace_period_due_date": (
                    state["grace_due_date"].isoformat()
                    if state["grace_due_date"] is not None
                    else None
                ),
                "payment_schedule": [
                    {
                        "month": month - first_calendar_month + 1,
                        "amount": number(amount),
                    }
                    for month, amount in sorted(state["schedule"].items())
                ],
                "payoff_month": state["payoff_month"],
                "interest": number(state["interest"]),
            }
            for state in sorted(states, key=lambda item: item["id"])
        ],
        "schedule": schedule,
        "warnings": warnings,
        "assumptions": [
            "APR is nominal annual percentage rate divided by 12",
            "interest is rounded to cents and applied before payment at each future month end",
            "avalanche, snowball, and custom keep each configured monthly payment available after a liability is repaid",
        ],
        "data_quality": "medium",
        "limitations": ["liability terms are user-provided"],
    }


def compare_debt_strategies(
    db: Any,
    monthly_extra_payment: float,
    debt_accounts: dict[str, Any],
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    plans = [
        plan_debt_payoff(
            db,
            monthly_extra_payment=0 if strategy == "minimum_only" else monthly_extra_payment,
            strategy=strategy,
            debt_accounts=debt_accounts,
            as_of=as_of,
        )
        for strategy in ("minimum_only", "snowball", "avalanche")
    ]
    configuration = next((plan for plan in plans if plan.get("status") == "configuration_required"), None)
    if configuration:
        return configuration
    strategies = [
        {
            "strategy": plan["strategy"],
            "months": plan["estimated_payoff_months"],
            "interest": plan["estimated_interest"],
        }
        for plan in plans
    ]
    if plans[0]["starting_debt"] == 0:
        return {
            "strategies": strategies,
            "best_by_interest": None,
            "best_by_duration": None,
            "criterion_notes": {
                "best_by_interest": "no active debt to compare",
                "best_by_duration": "no active debt to compare",
            },
            "data_quality": "high",
            "limitations": [],
        }
    finite = [item for item in strategies if item["months"] is not None]
    return {
        "strategies": strategies,
        "best_by_interest": min(finite, key=lambda item: (item["interest"], item["months"], item["strategy"]))["strategy"] if finite else None,
        "best_by_duration": min(finite, key=lambda item: (item["months"], item["interest"], item["strategy"]))["strategy"] if finite else None,
        "criterion_notes": {
            "best_by_interest": "lowest estimated total interest",
            "best_by_duration": "fewest estimated payoff months",
        },
        "data_quality": "medium",
        "limitations": ["APR and minimum payments are user-provided"],
    }
