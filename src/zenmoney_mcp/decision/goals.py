"""Zero-return goal planning with calendar month-end contributions."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_CEILING
from typing import Any

from ..planning import get_financial_snapshot
from ..validation import InputValidationError, parse_iso_date
from .models import CENT, ceiling_ratio, decimal_number, money, month_end_after, number


def _name(value: Any, field: str = "name") -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _contribution_months(today: date, deadline: date) -> int:
    months = 0
    while month_end_after(today, months + 1) <= deadline:
        months += 1
        if months > 1200:
            raise InputValidationError("target_date must be within 100 years")
    return months


def _required(gap: Decimal, months: int) -> Decimal | None:
    if gap == 0:
        return Decimal(0)
    if months == 0:
        return None
    return (gap / months).quantize(CENT, rounding=ROUND_CEILING)


def _goal_status(gap: Decimal, required: Decimal | None, allocated: Decimal) -> str:
    if gap == 0:
        return "funded"
    if required is None:
        return "impossible_deadline"
    if allocated >= required:
        return "on_track"
    return "underfunded"


def plan_financial_goal(
    db: Any,
    name: str,
    target_amount: float,
    current_amount: float = 0,
    target_date: str | None = None,
    monthly_contribution: float | None = None,
    priority: str = "medium",
    annual_return_pct: float = 0,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Solve either the required contribution or the completion date for one goal."""
    today = as_of or date.today()
    goal_name = _name(name)
    if priority not in {"low", "medium", "high"}:
        raise InputValidationError("priority must be low, medium, or high")
    target = money(decimal_number(target_amount, "target_amount", minimum=Decimal(0)))
    current = money(decimal_number(current_amount, "current_amount", minimum=Decimal(0)))
    annual_return = decimal_number(annual_return_pct, "annual_return_pct", minimum=Decimal(0))
    if annual_return != 0:
        raise InputValidationError("annual_return_pct must be 0 in Phase 3")
    if (target_date is None) == (monthly_contribution is None):
        raise InputValidationError(
            "provide exactly one of target_date or monthly_contribution"
        )
    gap = max(Decimal(0), target - current)
    snapshot = get_financial_snapshot(db, as_of=today)
    available = max(
        Decimal(0),
        Decimal(str(snapshot["cash_flow"]["trailing_3_months_average"]["net_cash_flow"])),
    )
    goal = {
        "name": goal_name,
        "target": number(target),
        "current": number(current),
        "gap": number(gap),
        "priority": priority,
    }
    common = {
        "goal": goal,
        "available_free_cash_flow": number(available),
        "assumptions": {
            "investment_return_pct": 0.0,
            "contribution_timing": "future calendar month ends",
        },
        "data_quality": "medium",
        "limitations": ["future free cash flow uses the trailing three completed-month average"],
    }
    if gap == 0:
        return {
            **common,
            "required_monthly_contribution": 0.0,
            "feasibility": "funded",
            "margin": number(available),
            "reasons": [],
        }

    if target_date is not None:
        deadline = parse_iso_date(target_date, "target_date")
        months = _contribution_months(today, deadline)
        required = _required(gap, months)
        if required is None:
            return {
                **common,
                "target_date": deadline.isoformat(),
                "contribution_months": 0,
                "required_monthly_contribution": None,
                "feasibility": "infeasible",
                "margin": None,
                "reasons": [
                    {
                        "metric": "contribution_months",
                        "actual": 0,
                        "target": 1,
                        "reason": "No future month-end contribution occurs by the deadline",
                    }
                ],
            }
        margin = money(available - required)
        return {
            **common,
            "target_date": deadline.isoformat(),
            "contribution_months": months,
            "required_monthly_contribution": number(required),
            "feasibility": "feasible" if margin >= 0 else "infeasible",
            "margin": number(margin),
            "reasons": [
                {
                    "metric": "monthly_contribution_capacity",
                    "actual": number(available),
                    "target": number(required),
                }
            ],
        }

    contribution = money(
        decimal_number(monthly_contribution, "monthly_contribution", minimum=Decimal(0))
    )
    if contribution == 0:
        months = None
        completion = None
        feasibility = "infeasible"
    else:
        months = ceiling_ratio(gap, contribution)
        completion = month_end_after(today, months).isoformat()
        feasibility = "feasible" if contribution <= available else "infeasible"
    return {
        **common,
        "monthly_contribution": number(contribution),
        "estimated_completion_months": months,
        "estimated_completion_date": completion,
        "feasibility": feasibility,
        "margin": number(available - contribution),
        "reasons": [
            {
                "metric": "monthly_contribution_capacity",
                "actual": number(available),
                "target": number(contribution),
            }
        ],
    }


def plan_multiple_goals(
    monthly_available: float,
    goals: list[dict[str, Any]],
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Allocate explicit monthly capacity greedily by numeric priority and input order."""
    today = as_of or date.today()
    available = money(decimal_number(monthly_available, "monthly_available", minimum=Decimal(0)))
    if not isinstance(goals, list) or len(goals) > 50:
        raise InputValidationError("goals must be an array with at most 50 items")
    prepared = []
    for index, raw in enumerate(goals):
        if not isinstance(raw, dict):
            raise InputValidationError(f"goals[{index}] must be an object")
        name = _name(raw.get("name"), f"goals[{index}].name")
        target = money(decimal_number(raw.get("target_amount"), f"goals[{index}].target_amount", minimum=Decimal(0)))
        current = money(decimal_number(raw.get("current_amount", 0), f"goals[{index}].current_amount", minimum=Decimal(0)))
        priority = raw.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 1:
            raise InputValidationError(f"goals[{index}].priority must be a positive integer")
        deadline = parse_iso_date(raw.get("target_date"), f"goals[{index}].target_date")
        gap = max(Decimal(0), target - current)
        months = _contribution_months(today, deadline)
        required = _required(gap, months)
        prepared.append(
            {
                "index": index,
                "name": name,
                "target_amount": target,
                "current_amount": current,
                "gap": gap,
                "target_date": deadline,
                "priority": priority,
                "required": required,
                "months": months,
                "allocated": Decimal(0),
            }
        )

    remaining = available
    for goal in sorted(prepared, key=lambda item: (item["priority"], item["index"])):
        if goal["required"] is None:
            continue
        goal["allocated"] = min(goal["required"], remaining)
        remaining -= goal["allocated"]

    finite_required = sum(
        (goal["required"] for goal in prepared if goal["required"] is not None),
        Decimal(0),
    )
    impossible = any(goal["required"] is None for goal in prepared)
    shortfall = max(Decimal(0), finite_required - available)
    alternatives = []
    payload_goals = []
    for goal in prepared:
        required = goal["required"]
        allocated = goal["allocated"]
        status = _goal_status(goal["gap"], required, allocated)
        payload_goals.append(
            {
                "name": goal["name"],
                "priority": goal["priority"],
                "target_amount": number(goal["target_amount"]),
                "current_amount": number(goal["current_amount"]),
                "target_date": goal["target_date"].isoformat(),
                "required_monthly": number(required) if required is not None else None,
                "allocated_monthly": number(allocated),
                "status": status,
            }
        )
        if required is not None and allocated < required:
            if allocated > 0:
                alternatives.append(
                    {
                        "type": "extend_deadline",
                        "goal": goal["name"],
                        "new_target_date": month_end_after(
                            today, ceiling_ratio(goal["gap"], allocated)
                        ).isoformat(),
                    }
                )
            else:
                alternatives.append(
                    {
                        "type": "increase_monthly_available",
                        "goal": goal["name"],
                        "additional_monthly": number(required),
                    }
                )

    return {
        "required_monthly_total": number(finite_required),
        "available_monthly": number(available),
        "shortfall": number(shortfall),
        "status": "infeasible" if shortfall or impossible else "feasible",
        "goals": payload_goals,
        "alternatives": alternatives,
        "allocation_policy": "ascending numeric priority; input order breaks ties",
        "assumptions": {"investment_return_pct": 0.0},
        "data_quality": "medium",
        "limitations": ["goal amounts, deadlines, and priorities are user-provided"],
    }
