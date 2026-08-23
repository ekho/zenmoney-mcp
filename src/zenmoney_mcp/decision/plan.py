"""Integrated financial-plan orchestration over facts and planning engines."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from ..planning import forecast_cash_flow, get_financial_snapshot
from ..validation import InputValidationError, bounded_int
from .debt import plan_debt_payoff
from .goals import plan_multiple_goals
from .models import ceiling_ratio, decimal_number, month_end_after, number
from .reserve import plan_emergency_fund

DEFAULT_PRIORITY_POLICY = [
    {"rank": 1, "type": "preserve_minimum_liquidity_buffer"},
    {"rank": 2, "type": "cover_essential_upcoming_obligations"},
    {"rank": 3, "type": "reach_minimum_emergency_fund"},
    {"rank": 4, "type": "meet_minimum_debt_payments"},
    {"rank": 5, "type": "reduce_expensive_debt"},
    {"rank": 6, "type": "fund_high_priority_goals"},
    {"rank": 7, "type": "allocate_remaining_free_cash_flow"},
]


def _recommendation(
    raw_cash_flow: Decimal,
    buffer_gap: Decimal,
    emergency_gap: Decimal,
    emergency_amount: Decimal,
    extra_debt: Decimal,
    debt_accounts: dict[str, Any],
    goal_plan: dict[str, Any],
    unallocated: Decimal,
) -> dict[str, Any]:
    common = {
        "assumptions": ["default priority policy is applied sequentially"],
        "tradeoffs": [],
        "alternatives": [],
    }
    if raw_cash_flow < 0:
        return {
            **common,
            "type": "restore_positive_cash_flow",
            "monthly_amount": number(abs(raw_cash_flow)),
            "reason": [
                {
                    "metric": "historical_net_cash_flow",
                    "actual": number(raw_cash_flow),
                    "target": 0.0,
                }
            ],
            "tradeoffs": ["new reserve, extra-debt, and goal contributions are deferred"],
            "alternatives": [
                {"type": "reduce_expenses_or_increase_income", "monthly_amount": number(abs(raw_cash_flow))}
            ],
        }
    if buffer_gap > 0:
        return {
            **common,
            "type": "preserve_minimum_liquidity_buffer",
            "monthly_amount": number(min(buffer_gap, emergency_amount)),
            "reason": [{"metric": "liquidity_buffer_gap", "actual": number(buffer_gap), "target": 0.0}],
            "tradeoffs": ["emergency-fund, extra-debt, and goal milestones move later"],
            "alternatives": [{"type": "lower_buffer", "requires_explicit_override": True}],
        }
    if emergency_gap > 0:
        slower = emergency_amount / 2
        return {
            **common,
            "type": "increase_emergency_fund",
            "monthly_amount": number(emergency_amount),
            "reason": [{"metric": "emergency_fund_gap", "actual": number(emergency_gap), "target": 0.0}],
            "tradeoffs": ["extra debt payoff and lower-priority goals receive no allocation until the reserve target is met"],
            "alternatives": [
                {
                    "type": "slower_reserve_build",
                    "monthly_amount": number(slower),
                    "completion_months": ceiling_ratio(emergency_gap, slower) if slower > 0 else None,
                }
            ],
        }
    if extra_debt > 0:
        highest_apr = max(
            (Decimal(str(value["apr_pct"])) for value in debt_accounts.values()),
            default=Decimal(0),
        )
        return {
            **common,
            "type": "reduce_expensive_debt",
            "monthly_amount": number(extra_debt),
            "reason": [{"metric": "highest_debt_apr_pct", "actual": number(highest_apr), "target": 0.0}],
            "tradeoffs": ["goal funding receives less monthly cash until debt is repaid"],
            "alternatives": [{"type": "minimum_only", "extra_payment": 0.0}],
        }
    underfunded = [goal for goal in goal_plan["goals"] if goal["status"] == "underfunded"]
    if underfunded:
        return {
            **common,
            "type": "resolve_goal_conflict",
            "goal": underfunded[0]["name"],
            "reason": [{"metric": "allocated_monthly", "actual": underfunded[0]["allocated_monthly"], "target": underfunded[0]["required_monthly"]}],
            "tradeoffs": ["meeting one deadline can delay a lower-priority goal"],
            "alternatives": goal_plan["alternatives"],
        }
    return {
        **common,
        "type": "allocate_remaining_free_cash_flow",
        "monthly_amount": number(unallocated),
        "reason": [{"metric": "unallocated_free_cash_flow", "actual": number(unallocated), "target": 0.0}],
        "tradeoffs": ["allocation requires a user-selected next objective"],
        "alternatives": [{"type": "keep_as_liquidity"}],
    }


def build_financial_plan(
    db: Any,
    planning_horizon_months: int = 24,
    minimum_liquidity_buffer: float = 100_000,
    emergency_fund: dict[str, Any] | None = None,
    debt_accounts: dict[str, Any] | None = None,
    goals: list[dict[str, Any]] | None = None,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Build a structured plan with one sequential allocation of monthly cash."""
    horizon = bounded_int(
        planning_horizon_months,
        "planning_horizon_months",
        default=24,
        minimum=1,
        maximum=120,
    )
    buffer = decimal_number(
        minimum_liquidity_buffer,
        "minimum_liquidity_buffer",
        minimum=Decimal(0),
    )
    if emergency_fund is None:
        emergency_fund = {}
    elif not isinstance(emergency_fund, dict):
        raise InputValidationError("emergency_fund must be an object")
    if not isinstance(debt_accounts, dict):
        raise InputValidationError("debt_accounts must be an object")
    if goals is None:
        goals = []
    elif not isinstance(goals, list):
        raise InputValidationError("goals must be an array")
    today = as_of or date.today()

    reserve = plan_emergency_fund(
        db,
        target_months=emergency_fund.get("target_months", 6),
        essential_category_ids=emergency_fund.get("essential_category_ids"),
        monthly_essential_override=emergency_fund.get("monthly_essential_override"),
        minimum_liquidity_buffer=minimum_liquidity_buffer,
        allocation_pct_of_free_cash_flow=100,
        include_restricted_deposits=emergency_fund.get("include_restricted_deposits", False),
        as_of=today,
    )
    debt = plan_debt_payoff(
        db,
        monthly_extra_payment=0,
        strategy="avalanche",
        debt_accounts=debt_accounts,
        as_of=today,
    )
    missing = []
    if reserve.get("status") == "configuration_required":
        missing.extend(reserve["missing"])
    if debt.get("status") == "configuration_required":
        missing.extend(debt["missing"])
    if missing:
        return {"status": "configuration_required", "missing": missing}

    snapshot = get_financial_snapshot(db, as_of=today)
    forecast = forecast_cash_flow(db, horizon_days=30, as_of=today)
    raw_cash_flow = Decimal(
        str(snapshot["cash_flow"]["trailing_3_months_average"]["net_cash_flow"])
    )
    available = max(Decimal(0), raw_cash_flow)
    first_debt_month = debt["schedule"][0] if debt["schedule"] else None
    minimum_required = Decimal(
        str(first_debt_month["total_payment"] if first_debt_month else 0)
    )
    minimum_paid = min(available, minimum_required)
    remaining = available - minimum_paid

    projected_liquidity = Decimal(
        str(forecast["scenarios"]["scheduled_only"]["ending_liquid_funds"])
    )
    buffer_gap = max(Decimal(0), buffer - projected_liquidity)
    liquidity_allocation = min(remaining, buffer_gap)
    remaining -= liquidity_allocation

    emergency_gap = Decimal(str(reserve["target"]["gap"]))
    remaining_emergency_gap = max(Decimal(0), emergency_gap - liquidity_allocation)
    emergency_allocation = min(remaining, remaining_emergency_gap)
    remaining -= emergency_allocation

    starting_debt = Decimal(str(debt["starting_debt"]))
    first_interest = Decimal(
        str(first_debt_month["total_interest"] if first_debt_month else 0)
    )
    extra_debt = min(
        remaining,
        max(Decimal(0), starting_debt + first_interest - minimum_paid),
    )
    remaining -= extra_debt

    goal_plan = plan_multiple_goals(number(remaining), goals, as_of=today)
    goal_allocations = [
        {
            "name": goal["name"],
            "monthly_amount": goal["allocated_monthly"],
            "status": goal["status"],
        }
        for goal in goal_plan["goals"]
    ]
    allocated_goals = sum(
        (Decimal(str(goal["monthly_amount"])) for goal in goal_allocations),
        Decimal(0),
    )
    remaining -= allocated_goals

    warnings = []
    if raw_cash_flow < 0:
        warnings.append("negative_free_cash_flow")
    if minimum_paid < minimum_required:
        warnings.append("minimum_debt_payment_shortfall")
    if buffer_gap > liquidity_allocation:
        warnings.append("minimum_liquidity_buffer_not_funded_in_one_month")

    priorities = [
        {"rank": 1, "type": "liquidity", "status": "below_buffer" if buffer_gap else "covered"},
        {"rank": 2, "type": "upcoming_obligations", "status": "covered" if projected_liquidity >= buffer else "at_risk"},
        {"rank": 3, "type": "emergency_fund", "status": "below_target" if emergency_gap else "funded"},
        {"rank": 4, "type": "minimum_debt_payments", "status": "shortfall" if minimum_paid < minimum_required else "covered" if minimum_required else "no_debt"},
        {"rank": 5, "type": "expensive_debt", "status": "active" if starting_debt else "no_debt"},
        {"rank": 6, "type": "goals", "status": goal_plan["status"] if goals else "no_goals"},
        {"rank": 7, "type": "remaining_cash_flow", "status": "unallocated" if remaining else "allocated"},
    ]

    milestones = []
    reserve_contribution = liquidity_allocation + emergency_allocation
    if emergency_gap and reserve_contribution:
        months = ceiling_ratio(emergency_gap, reserve_contribution)
        milestones.append(
            {
                "type": "emergency_fund",
                "estimated_months": months,
                "estimated_date": month_end_after(today, months).isoformat(),
            }
        )
    if starting_debt and extra_debt:
        accelerated = plan_debt_payoff(
            db,
            monthly_extra_payment=number(extra_debt),
            strategy="avalanche",
            debt_accounts=debt_accounts,
            as_of=today,
        )
        milestones.append(
            {
                "type": "debt_payoff",
                "estimated_months": accelerated["estimated_payoff_months"],
                "estimated_interest": accelerated["estimated_interest"],
            }
        )
    milestones.extend(
        {
            "type": "goal",
            "goal": goal["name"],
            "status": goal["status"],
            "target_date": goal["target_date"],
        }
        for goal in goal_plan["goals"]
    )

    return {
        "as_of": today.isoformat(),
        "planning_horizon_months": horizon,
        "currency": snapshot["currency"],
        "current_position": snapshot,
        "priorities": priorities,
        "priority_policy": DEFAULT_PRIORITY_POLICY,
        "monthly_allocation": {
            "historical_net_cash_flow": number(raw_cash_flow),
            "minimum_debt_payments": number(minimum_paid),
            "free_cash_flow": number(max(Decimal(0), raw_cash_flow - minimum_paid)),
            "liquidity_buffer": number(liquidity_allocation),
            "emergency_fund": number(emergency_allocation),
            "extra_debt_payment": number(extra_debt),
            "goals": goal_allocations,
            "unallocated": number(remaining),
        },
        "milestones": milestones,
        "constraints": [
            {"type": "minimum_liquidity_buffer", "amount": number(buffer)},
            {"type": "monthly_cash_available", "amount": number(max(Decimal(0), raw_cash_flow))},
            {"type": "planning_horizon", "months": horizon},
        ],
        "recommended_action": _recommendation(
            raw_cash_flow,
            buffer_gap,
            emergency_gap,
            reserve_contribution,
            extra_debt,
            debt_accounts,
            goal_plan,
            remaining,
        ),
        "assumptions": [
            "minimum debt payments are deducted before discretionary free-cash-flow allocation",
            "the visible default priority policy is applied sequentially",
            "investment return is zero",
            "future contributions occur at calendar month ends",
        ],
        "warnings": warnings,
        "data_quality": "medium",
        "limitations": [
            "essential spending, APR, minimum payments, and goals are user-provided",
            "historical trailing-three-month net cash flow is used as monthly capacity",
        ],
    }
