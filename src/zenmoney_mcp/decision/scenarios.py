"""Reusable deterministic monthly scenario engine."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from ..planning import get_debt_service, get_financial_snapshot
from ..validation import InputValidationError, bounded_int
from .models import HUNDRED, decimal_number, money, month_end_after, number

SCENARIO_NAMES = {"negative", "base", "positive"}


def _percentage(value: Any, field: str) -> Decimal:
    result = decimal_number(value, field)
    if result < -HUNDRED:
        raise InputValidationError(f"{field} must be at least -100")
    return result


def _one_time_expenses(
    raw: Any, horizon: int
) -> dict[int, Decimal]:
    if raw is None:
        return {}
    if not isinstance(raw, list) or len(raw) > 120:
        raise InputValidationError("one_time_expenses must contain at most 120 items")
    result: dict[int, Decimal] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InputValidationError(f"one_time_expenses[{index}] must be an object")
        month = bounded_int(
            item.get("month"),
            f"one_time_expenses[{index}].month",
            minimum=1,
            maximum=horizon,
        )
        amount = money(
            decimal_number(
                item.get("amount"),
                f"one_time_expenses[{index}].amount",
                minimum=Decimal(0),
            )
        )
        result[month] = result.get(month, Decimal(0)) + amount
    return result


def _goals(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 50:
        raise InputValidationError("scenario.goals must contain at most 50 items")
    result = []
    names = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InputValidationError(f"scenario.goals[{index}] must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise InputValidationError("scenario goal names must be non-empty and unique")
        names.add(name)
        target = money(
            decimal_number(
                item.get("target_amount"),
                f"scenario.goals[{index}].target_amount",
                minimum=Decimal(0),
            )
        )
        current = money(
            decimal_number(
                item.get("current_amount", 0),
                f"scenario.goals[{index}].current_amount",
                minimum=Decimal(0),
            )
        )
        contribution = money(
            decimal_number(
                item.get("monthly_contribution"),
                f"scenario.goals[{index}].monthly_contribution",
                minimum=Decimal(0),
            )
        )
        result.append(
            {
                "name": name,
                "target": target,
                "balance": min(current, target),
                "contribution": contribution,
            }
        )
    return result


def run_financial_scenario(
    db: Any,
    horizon_months: int,
    scenario: dict[str, Any],
    scenario_name: str = "base",
    minimum_liquidity_buffer: float = 0,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Project cash, debt, goal balances, and net worth through month-end snapshots."""
    horizon = bounded_int(
        horizon_months, "horizon_months", minimum=1, maximum=120
    )
    if scenario_name not in SCENARIO_NAMES:
        raise InputValidationError(
            f"scenario_name must be one of {sorted(SCENARIO_NAMES)}"
        )
    if not isinstance(scenario, dict):
        raise InputValidationError("scenario must be an object")
    today = as_of or date.today()
    income_change = _percentage(scenario.get("income_change_pct", 0), "income_change_pct")
    expense_change = _percentage(scenario.get("expense_change_pct", 0), "expense_change_pct")
    one_times = _one_time_expenses(scenario.get("one_time_expenses"), horizon)
    extra_payment = money(
        decimal_number(
            scenario.get("monthly_extra_debt_payment", 0),
            "monthly_extra_debt_payment",
            minimum=Decimal(0),
        )
    )
    buffer = money(
        decimal_number(
            minimum_liquidity_buffer,
            "minimum_liquidity_buffer",
            minimum=Decimal(0),
        )
    )
    goals = _goals(scenario.get("goals"))

    snapshot = get_financial_snapshot(db, as_of=today)
    debt_service = get_debt_service(db, as_of=today)
    cash = money(snapshot["own_liquid_funds"] + snapshot["accessible_savings"])
    debt = money(debt_service["current_debt_balance"])
    net_worth = money(snapshot["net_worth"])
    starting = {
        "liquid_funds": number(cash),
        "debt": number(debt),
        "net_worth": number(net_worth),
        "goal_balances": {goal["name"]: number(goal["balance"]) for goal in goals},
    }
    baseline = snapshot["cash_flow"]["trailing_3_months_average"]
    monthly_income = money(
        Decimal(str(baseline["income"])) * (HUNDRED + income_change) / HUNDRED
    )
    monthly_expenses = money(
        Decimal(str(baseline["outcome"])) * (HUNDRED + expense_change) / HUNDRED
    )

    minimum_cash = cash
    minimum_month = 0
    months_below_buffer = []
    cash_flow = []
    for month in range(1, horizon + 1):
        opening_cash = cash
        one_time = money(one_times.get(month, Decimal(0)))
        debt_payment = min(extra_payment, debt)
        debt = money(debt - debt_payment)
        goal_contributions = {}
        total_goals = Decimal(0)
        for goal in goals:
            contribution = min(goal["contribution"], goal["target"] - goal["balance"])
            goal["balance"] = money(goal["balance"] + contribution)
            goal_contributions[goal["name"]] = number(contribution)
            total_goals += contribution
        cash = money(
            cash
            + monthly_income
            - monthly_expenses
            - one_time
            - debt_payment
            - total_goals
        )
        net_worth = money(net_worth + monthly_income - monthly_expenses - one_time)
        if cash < minimum_cash:
            minimum_cash, minimum_month = cash, month
        if cash < buffer:
            months_below_buffer.append(month)
        cash_flow.append(
            {
                "month": month,
                "date": month_end_after(today, month).isoformat(),
                "month_start_balance": number(opening_cash),
                "income": number(monthly_income),
                "baseline_expenses": number(monthly_expenses),
                "one_time_expenses": number(one_time),
                "extra_debt_payment": number(debt_payment),
                "goal_contributions": goal_contributions,
                "month_end_balance": number(cash),
                "ending_debt": number(debt),
            }
        )

    warnings = []
    if minimum_cash < 0:
        warnings.append("liquidity_below_zero")
    if months_below_buffer:
        warnings.append("minimum_liquidity_buffer_breached")
    if extra_payment and starting["debt"]:
        warnings.append("debt_interest_not_reforecasted")
    return {
        "horizon_months": horizon,
        "scenario_name": scenario_name,
        "currency": snapshot["currency"],
        "starting_position": starting,
        "ending_position": {
            "liquid_funds": number(cash),
            "debt": number(debt),
            "net_worth": number(net_worth),
            "goal_balances": {
                goal["name"]: number(goal["balance"]) for goal in goals
            },
        },
        "minimum_liquidity": {
            "amount": number(minimum_cash),
            "month": minimum_month,
        },
        "months_below_buffer": months_below_buffer,
        "cash_flow": cash_flow,
        "warnings": warnings,
        "assumptions": [
            "historical trailing-three-month income and spending repeat each calendar month",
            "percentage changes remain constant for the full horizon",
            "extra debt payments reduce principal directly; debt interest is not reforecast without a full amortization configuration",
            "goal contributions remain part of net worth and cannot exceed the goal target",
        ],
        "data_quality": "medium",
        "limitations": [
            "scenario inputs are deterministic and user-provided",
            "scheduled reminders beyond Phase 2 forecast horizons are not inferred",
        ],
    }
