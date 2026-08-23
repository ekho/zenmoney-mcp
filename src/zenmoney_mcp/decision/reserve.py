"""Emergency-fund planning composed from Phase 2 facts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from ..financial_correctness import get_liquidity
from ..planning import get_emergency_fund_status, get_financial_snapshot
from ..validation import InputValidationError, bounded_int
from .models import HUNDRED, ceiling_ratio, decimal_number, money, month_end_after, number


def plan_emergency_fund(
    db: Any,
    target_months: int = 6,
    essential_category_ids: list[str] | None = None,
    monthly_essential_override: float | None = None,
    minimum_liquidity_buffer: float = 0,
    allocation_pct_of_free_cash_flow: float = 75,
    include_restricted_deposits: bool = False,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Build an explainable reserve plan without treating credit as savings."""
    today = as_of or date.today()
    target_months = bounded_int(
        target_months, "target_months", default=6, minimum=1, maximum=60
    )
    buffer = decimal_number(minimum_liquidity_buffer, "minimum_liquidity_buffer", minimum=Decimal(0))
    allocation = decimal_number(
        allocation_pct_of_free_cash_flow,
        "allocation_pct_of_free_cash_flow",
        minimum=Decimal(0),
    )
    if allocation > HUNDRED:
        raise InputValidationError("allocation_pct_of_free_cash_flow must be at most 100")

    status = get_emergency_fund_status(
        db,
        essential_category_ids=essential_category_ids,
        monthly_essential_override=monthly_essential_override,
        target_months=target_months,
        as_of=today,
    )
    if status.get("status") == "configuration_required" and not status.get("currency"):
        return {
            "status": "configuration_required",
            "missing": [
                {
                    "field": "essential_category_ids or monthly_essential_override",
                    "reason": "Required to calculate essential monthly spending",
                }
            ],
        }

    liquidity = get_liquidity(db)
    eligible = Decimal(str(status["reserve"]["total_eligible"]))
    if include_restricted_deposits:
        eligible += Decimal(str(liquidity["restricted_savings"]))
    monthly_essential = Decimal(str(status["monthly_essential_baseline"]))
    target = money(monthly_essential * target_months)
    gap = max(Decimal(0), target - eligible)
    coverage = eligible / monthly_essential if monthly_essential else None

    snapshot = get_financial_snapshot(db, as_of=today)
    free_cash_flow = max(
        Decimal(0),
        Decimal(str(snapshot["cash_flow"]["trailing_3_months_average"]["net_cash_flow"])),
    )
    contribution = money(free_cash_flow * allocation / HUNDRED)
    if gap == 0:
        plan_status, months, completion = "funded", 0, None
    elif contribution == 0:
        plan_status, months, completion = "insufficient_capacity", None, None
    else:
        months = ceiling_ratio(gap, contribution)
        plan_status = "building"
        completion = month_end_after(today, months).isoformat()

    liquid_own = Decimal(str(liquidity["liquid_own"]))
    reasons = []
    if gap:
        reasons.append(
            {
                "metric": "emergency_fund_months",
                "actual": round(float(coverage), 2) if coverage is not None else None,
                "target": float(target_months),
            }
        )
    if liquid_own < buffer:
        reasons.append(
            {
                "metric": "liquid_own",
                "actual": number(liquid_own),
                "target": number(buffer),
            }
        )

    return {
        "currency": status["currency"],
        "current": {
            "eligible_reserve": number(eligible),
            "coverage_months": round(float(coverage), 2) if coverage is not None else None,
        },
        "target": {
            "months": target_months,
            "amount": number(target),
            "gap": number(gap),
        },
        "capacity": {
            "monthly_free_cash_flow": number(free_cash_flow),
            "allocation_pct": number(allocation),
            "monthly_contribution": number(contribution),
        },
        "plan": {
            "status": plan_status,
            "estimated_months_to_target": months,
            "estimated_completion_date": completion,
        },
        "constraints": {
            "minimum_liquidity_buffer": number(buffer),
            "liquidity_buffer_gap": number(max(Decimal(0), buffer - liquid_own)),
            "restricted_deposits_included": include_restricted_deposits,
            "credit_included": False,
        },
        "reasons": reasons,
        "assumptions": [
            "future monthly free cash flow equals the trailing three completed-month average",
            "contributions occur at future calendar month ends",
            "investment return is zero",
        ],
        "data_quality": "medium",
        "limitations": [
            "essential spending classification is user-provided"
            if essential_category_ids
            else "monthly essential spending is user-provided"
        ],
    }
