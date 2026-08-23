from __future__ import annotations

from types import SimpleNamespace

import pytest

from zenmoney_mcp.financial_correctness import (
    analyze_income,
    analyze_trends,
    configure_legacy_analytics,
    detect_recurring,
    get_upcoming_payments,
)
from zenmoney_mcp.validation import InputValidationError


class Legacy:
    def __init__(self):
        self.calls = []

    def analyze_income(self, db, **kwargs):
        self.calls.append(("analyze_income", kwargs))
        return kwargs

    def analyze_trends(self, db, **kwargs):
        self.calls.append(("analyze_trends", kwargs))
        return kwargs

    def detect_recurring(self, db, **kwargs):
        self.calls.append(("detect_recurring", kwargs))
        return kwargs

    def get_upcoming_payments(self, db, **kwargs):
        self.calls.append(("get_upcoming_payments", kwargs))
        return kwargs


def test_wrappers_reject_unbounded_or_unknown_inputs_and_delegate_valid_values():
    legacy = Legacy()
    configure_legacy_analytics(legacy)

    with pytest.raises(InputValidationError, match="months"):
        analyze_trends(object(), months=0)
    with pytest.raises(InputValidationError, match="metric"):
        analyze_trends(object(), months=6, metric="profit")
    with pytest.raises(InputValidationError, match="lookback_months"):
        detect_recurring(object(), lookback_months=0)
    with pytest.raises(InputValidationError, match="days_ahead"):
        get_upcoming_payments(object(), days_ahead=1000)

    result = analyze_income(
        object(),
        period="2026-08",
        start_date="2026-08-01",
        end_date="2026-08-21",
        top_n=5,
    )
    assert result["top_n"] == 5
    assert legacy.calls[-1][0] == "analyze_income"
