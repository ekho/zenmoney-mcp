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


def _configuration(
    accounts: list[dict[str, Any]],
    configured: dict[str, Any],
    strategy: str,
) -> list[dict[str, str]]:
    missing = []
    for account in accounts:
        account_id = account["id"]
        values = configured.get(account_id)
        if not isinstance(values, dict):
            missing.append(
                {
                    "field": f"debt_accounts.{account_id}",
                    "reason": "Required for an active debt account",
                }
            )
            continue
        if "apr_pct" not in values:
            missing.append(
                {
                    "field": f"debt_accounts.{account_id}.apr_pct",
                    "reason": (
                        "Required to calculate interest and avalanche priority"
                        if strategy == "avalanche"
                        else "Required to calculate interest"
                    ),
                }
            )
        if "minimum_payment" not in values:
            missing.append(
                {
                    "field": f"debt_accounts.{account_id}.minimum_payment",
                    "reason": "Required to calculate the monthly debt budget",
                }
            )
    return missing


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
    configured = debt_accounts or {}
    if not isinstance(configured, dict) or len(configured) > 50:
        raise InputValidationError("debt_accounts must be an object with at most 50 accounts")

    facts = get_debt_service(db, as_of=as_of)
    active = [
        {
            "id": obligation["account_id"],
            "title": obligation["title"],
            "debt_balance": obligation["balance"],
        }
        for obligation in facts["obligations"]
        if obligation["source_account_type"] in {"loan", "debt"}
    ]
    if len(active) > 50:
        raise InputValidationError("at most 50 active debt accounts are supported")
    if not active:
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

    missing = _configuration(active, configured, strategy)
    if strategy == "custom":
        active_ids = {account["id"] for account in active}
        if not isinstance(custom_order, list) or set(custom_order) != active_ids or len(custom_order) != len(active_ids):
            missing.append(
                {
                    "field": "custom_order",
                    "reason": "Must list every active debt account exactly once",
                }
            )
    if missing:
        return {"status": "configuration_required", "missing": missing}

    states = []
    for account in active:
        values = configured[account["id"]]
        apr = decimal_number(values["apr_pct"], f"debt_accounts.{account['id']}.apr_pct", minimum=Decimal(0))
        minimum = money(decimal_number(values["minimum_payment"], f"debt_accounts.{account['id']}.minimum_payment", minimum=Decimal(0)))
        states.append(
            {
                "id": account["id"],
                "title": account["title"],
                "balance": money(account["debt_balance"]),
                "apr": apr,
                "minimum": minimum,
                "interest": Decimal(0),
                "payoff_month": None,
            }
        )

    minimum_budget = sum((state["minimum"] for state in states), Decimal(0))
    applied_extra = Decimal(0) if strategy == "minimum_only" else money(extra)
    total_budget = minimum_budget + applied_extra
    starting_debt = sum((state["balance"] for state in states), Decimal(0))
    schedule = []
    warnings: list[str] = []

    for month in range(1, MAX_PAYOFF_MONTHS + 1):
        rows: dict[str, dict[str, Decimal | str]] = {}
        for state in states:
            opening = state["balance"]
            interest = money(opening * state["apr"] / HUNDRED / 12)
            due = opening + interest
            payment = min(state["minimum"], due)
            state["balance"] = money(due - payment)
            state["interest"] += interest
            rows[state["id"]] = {
                "account_id": state["id"],
                "opening_balance": opening,
                "interest": interest,
                "payment": payment,
            }

        if strategy != "minimum_only":
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
                "date": month_end_after(as_of or date.today(), month).isoformat(),
                "accounts": month_rows,
                "total_payment": number(sum((Decimal(str(row["payment"])) for row in month_rows), Decimal(0))),
                "total_interest": number(sum((Decimal(str(row["interest"])) for row in month_rows), Decimal(0))),
                "ending_debt": number(ending_debt),
            }
        )
        if ending_debt == 0:
            payoff_months: int | None = month
            break
        if no_positive_principal:
            payoff_months = None
            break
    else:
        payoff_months = None
        warnings.append("payoff_horizon_exceeded")

    return {
        "strategy": strategy,
        "currency": facts["currency"],
        "starting_debt": number(starting_debt),
        "monthly_budget": {
            "minimum_payments": number(minimum_budget),
            "extra_payment": number(applied_extra),
            "total": number(total_budget),
        },
        "estimated_payoff_months": payoff_months,
        "estimated_interest": number(sum((state["interest"] for state in states), Decimal(0))),
        "accounts": [
            {
                "account_id": state["id"],
                "title": state["title"],
                "starting_balance": number(next(account["debt_balance"] for account in active if account["id"] == state["id"])),
                "apr_pct": number(state["apr"]),
                "minimum_payment": number(state["minimum"]),
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
            "avalanche and snowball keep the configured total monthly budget after an account is repaid",
        ],
        "data_quality": "medium",
        "limitations": ["APR and minimum payments are user-provided"],
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
