from datetime import date

import pytest

from zenmoney_mcp import planning
from zenmoney_mcp.hardened_database import CurrencyRateError, HardenedDatabase
from zenmoney_mcp.money import FinancialDataError, convert, user_currency
from zenmoney_mcp.periods import completed_periods
from zenmoney_mcp.planning import (
    compare_periods,
    forecast_cash_flow,
    get_cash_flow,
    get_debt_service,
    get_emergency_fund_status,
    get_financial_snapshot,
    get_spending_baseline,
)
from zenmoney_mcp.validation import InputValidationError


@pytest.fixture
def planning_db() -> HardenedDatabase:
    db = HardenedDatabase(":memory:")
    db.init_schema()
    conn = db.connect()
    conn.executemany(
        "INSERT INTO instruments(id,title,short_title,symbol,rate,changed) "
        "VALUES (?,?,?,?,?,1)",
        [
            (1, "Ruble", "RUB", "₽", 1.0),
            (2, "US Dollar", "USD", "$", 90.0),
        ],
    )
    conn.execute(
        "INSERT INTO users(id,login,currency,parent,month_start_day,changed) "
        "VALUES (1,'u',1,NULL,1,1)"
    )
    conn.executemany(
        "INSERT INTO accounts(id,title,type,instrument,balance,credit_limit,in_balance,savings,archive,user,changed) "
        "VALUES (?,?,?,?,?,?,?,?,?,1,1)",
        [
            ("cash-rub", "Cash RUB", "checking", 1, 10_000, None, 1, 0, 0),
            ("cash-usd", "Cash USD", "cash", 2, 100, None, 1, 0, 0),
            ("excluded", "Excluded", "checking", 1, 2_000, None, 0, 0, 0),
            ("savings", "Savings", "checking", 1, 5_000, None, 1, 1, 0),
            ("deposit", "Deposit", "deposit", 1, 20_000, None, 1, 1, 0),
            ("credit", "Credit", "ccard", 1, -1_000, 50_000, 1, 0, 0),
            ("loan", "Loan", "loan", 1, -6_000, None, 1, 0, 0),
        ],
    )
    conn.executemany(
        "INSERT INTO tags(id,title,parent,show_income,show_outcome,user,changed) "
        "VALUES (?,?,?,?,?,1,1)",
        [
            ("food", "Food", None, 0, 1),
            ("groceries", "Groceries", "food", 0, 1),
            ("transport", "Transport", None, 0, 1),
            ("salary", "Salary", None, 1, 0),
        ],
    )
    conn.commit()
    return db


def add_transaction(
    db,
    tx_id,
    tx_date,
    *,
    income=0,
    outcome=0,
    income_instrument=1,
    outcome_instrument=1,
    income_account="cash-rub",
    outcome_account="cash-rub",
    tag=None,
    hold=0,
    payee=None,
    merchant=None,
    reminder_marker=None,
):
    db.connect().execute(
        """INSERT INTO transactions(
            id,date,user,deleted,hold,income,income_instrument,income_account,
            outcome,outcome_instrument,outcome_account,tag,payee,merchant,
            reminder_marker,changed
        ) VALUES (?,?,1,0,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (
            tx_id,
            tx_date,
            hold,
            income,
            income_instrument,
            income_account,
            outcome,
            outcome_instrument,
            outcome_account,
            f'["{tag}"]' if tag else None,
            payee,
            merchant,
            reminder_marker,
        ),
    )


def add_marker(
    db,
    marker_id,
    marker_date,
    *,
    income=0,
    outcome=0,
    income_instrument=None,
    outcome_instrument=None,
    income_account="cash-rub",
    outcome_account="cash-rub",
    payee=None,
):
    db.connect().execute(
        """INSERT INTO reminder_markers(
            id,user,date,state,income,outcome,income_instrument,outcome_instrument,
            income_account,outcome_account,payee,changed
        ) VALUES (?,1,?,'planned',?,?,?,?,?,?,?,1)""",
        (
            marker_id,
            marker_date,
            income,
            outcome,
            income_instrument,
            outcome_instrument,
            income_account,
            outcome_account,
            payee,
        ),
    )


def test_completed_periods_respect_month_start_day(planning_db):
    planning_db.connect().execute("UPDATE users SET month_start_day=8")

    periods = completed_periods(planning_db, 2, date(2026, 8, 23))

    assert [(p.start.isoformat(), p.end.isoformat()) for p in periods] == [
        ("2026-06-08", "2026-07-07"),
        ("2026-07-08", "2026-08-07"),
    ]


def test_convert_rejects_missing_rate(planning_db):
    planning_db.connect().execute("UPDATE instruments SET rate=NULL WHERE id=2")

    with pytest.raises(CurrencyRateError):
        convert(planning_db, 10, 2, user_currency(planning_db))


def test_convert_zero_does_not_require_stale_instrument(planning_db):
    assert convert(planning_db, 0, 999, user_currency(planning_db)) == 0


def test_financial_obligations_include_every_active_negative_account(planning_db):
    planning_db.connect().executemany(
        "INSERT INTO accounts(id,title,type,instrument,balance,in_balance,savings,archive,user,changed) "
        "VALUES (?,?,?,?,?,?,0,?,1,1)",
        [
            ("installment", "Installment", "checking", 1, -300, 0, 0),
            ("personal", "Personal", "debt", 1, -200, 1, 0),
            ("odd", "Odd", "cash", 1, -100, 1, 0),
            ("positive", "Positive", "loan", 1, 100, 1, 0),
            ("zero", "Zero", "debt", 1, 0, 1, 0),
            ("archived", "Archived", "loan", 1, -400, 1, 1),
        ],
    )

    obligations = planning._financial_obligations(
        planning_db, user_currency(planning_db), as_of=date(2026, 8, 23)
    )

    assert [item["account_id"] for item in obligations] == [
        "credit",
        "installment",
        "loan",
        "odd",
        "personal",
    ]
    by_id = {item["account_id"]: item for item in obligations}
    assert by_id["loan"]["classification"] == "loan"
    assert by_id["credit"]["classification"] == "credit_card"
    assert by_id["personal"]["classification"] == "personal_debt"
    assert by_id["installment"]["classification"] == "other"
    assert by_id["installment"]["classification_confidence"] == "low"
    assert by_id["installment"]["in_balance"] is False
    assert by_id["installment"]["balance"] == 300
    assert by_id["installment"]["minimum_payment"] == {
        "amount": None,
        "due_date": None,
        "source": "unknown",
        "confidence": "low",
    }
    assert by_id["installment"]["apr_pct"] == {
        "value": None,
        "source": "unknown",
    }
    assert by_id["odd"]["classification"] == "other"


def test_financial_obligation_overrides_are_explicit(planning_db):
    obligations = planning._financial_obligations(
        planning_db,
        user_currency(planning_db),
        {
            "credit": {
                "classification": "installment",
                "minimum_payment": {"amount": 250, "due_date": "2026-09-18"},
                "apr_pct": 19.9,
            }
        },
        as_of=date(2026, 8, 23),
    )

    credit = next(item for item in obligations if item["account_id"] == "credit")
    assert credit["classification"] == "installment"
    assert credit["classification_confidence"] == "high"
    assert credit["minimum_payment"] == {
        "amount": 250,
        "due_date": "2026-09-18",
        "source": "user_override",
        "confidence": "high",
    }
    assert credit["apr_pct"] == {"value": 19.9, "source": "user_override"}


@pytest.mark.parametrize(
    ("overrides", "error_path"),
    [
        ({"missing": {"classification": "loan"}}, "obligation_overrides.missing"),
        (
            {"cash-rub": {"classification": "loan"}},
            "obligation_overrides.cash-rub",
        ),
        ({"credit": {"guess": "loan"}}, "obligation_overrides.credit.guess"),
        (
            {"credit": {"classification": "mortgage"}},
            "obligation_overrides.credit.classification",
        ),
        (
            {"credit": {"classification": []}},
            "obligation_overrides.credit.classification",
        ),
        (
            {"credit": {"minimum_payment": {"amount": -1}}},
            "obligation_overrides.credit.minimum_payment.amount",
        ),
        (
            {
                "credit": {
                    "minimum_payment": {"amount": 1, "due_date": "2026-02-30"}
                }
            },
            "obligation_overrides.credit.minimum_payment.due_date",
        ),
        (
            {str(index): {"classification": "loan"} for index in range(51)},
            "obligation_overrides",
        ),
    ],
)
def test_financial_obligation_overrides_are_strict(
    planning_db, overrides, error_path
):
    with pytest.raises(InputValidationError, match=error_path):
        planning._financial_obligations(
            planning_db,
            user_currency(planning_db),
            overrides,
            as_of=date(2026, 8, 23),
        )


def test_financial_obligation_uses_nearest_reminder_payment(planning_db):
    add_marker(
        planning_db,
        "excluded-source",
        "2026-09-01",
        income=100,
        outcome=100,
        income_instrument=1,
        outcome_instrument=1,
        income_account="loan",
        outcome_account="excluded",
    )
    add_marker(
        planning_db,
        "nearest",
        "2026-09-18",
        income=500,
        outcome=500,
        income_instrument=1,
        outcome_instrument=1,
        income_account="loan",
        outcome_account="cash-rub",
    )
    add_marker(
        planning_db,
        "later",
        "2026-10-18",
        income=700,
        outcome=700,
        income_instrument=1,
        outcome_instrument=1,
        income_account="loan",
        outcome_account="cash-rub",
    )

    obligations = planning._financial_obligations(
        planning_db, user_currency(planning_db), as_of=date(2026, 8, 23)
    )

    loan = next(item for item in obligations if item["account_id"] == "loan")
    assert loan["minimum_payment"] == {
        "amount": 500,
        "due_date": "2026-09-18",
        "source": "reminder",
        "confidence": "medium",
    }


def test_financial_obligation_payment_override_skips_reminder_conversion(planning_db):
    add_marker(
        planning_db,
        "usd-reminder",
        "2026-09-18",
        income=1,
        outcome=1,
        income_instrument=2,
        outcome_instrument=2,
        income_account="loan",
        outcome_account="cash-usd",
    )
    planning_db.connect().execute("UPDATE instruments SET rate=NULL WHERE id=2")

    obligations = planning._financial_obligations(
        planning_db,
        user_currency(planning_db),
        {"loan": {"minimum_payment": {"amount": 250}}},
        as_of=date(2026, 8, 23),
    )

    loan = next(item for item in obligations if item["account_id"] == "loan")
    assert loan["minimum_payment"] == {
        "amount": 250,
        "due_date": None,
        "source": "user_override",
        "confidence": "high",
    }


def test_cash_flow_excludes_transfers_holds_and_external_accounts(planning_db):
    add_transaction(planning_db, "income", "2026-08-01", income=1_000, tag="salary")
    add_transaction(planning_db, "expense", "2026-08-02", outcome=200, tag="food")
    add_transaction(planning_db, "transfer", "2026-08-03", income=300, outcome=300)
    add_transaction(
        planning_db,
        "asset-transfer",
        "2026-08-03",
        income=400,
        outcome=400,
        income_account="savings",
    )
    add_transaction(planning_db, "hold", "2026-08-04", outcome=50, hold=1)
    add_transaction(
        planning_db, "external", "2026-08-05", outcome=70, outcome_account="excluded"
    )

    result = get_cash_flow(
        planning_db,
        start_date="2026-08-01",
        end_date="2026-08-31",
        as_of=date(2026, 9, 1),
    )

    assert result["income"] == 1_000
    assert result["operating_expenses"] == 200
    assert result["operating_net_cash_flow"] == 800
    assert result["financing_inflow"] == 0
    assert result["debt_service_cash_outflow"] == 0
    assert result["net_cash_flow_after_debt_service"] == 800
    assert result["savings_rate_before_debt_service_pct"] == 80
    assert result["savings_rate_after_debt_service_pct"] == 80
    assert result["flow_components"]["income"] == {"amount": 1_000, "count": 1}
    assert result["flow_components"]["operating_expense"] == {
        "amount": 200,
        "count": 1,
    }
    assert result["flow_components"]["internal_transfer"] == {
        "amount": 300,
        "count": 1,
    }
    assert result["flow_components"]["asset_transfer"] == {
        "amount": 400,
        "count": 1,
    }
    assert result["uncertain_transactions"] == []
    assert "outcome" not in result
    assert "net_cash_flow" not in result
    assert "savings_rate_pct" not in result


def test_cash_flow_converts_each_side_to_user_currency(planning_db):
    add_transaction(
        planning_db,
        "usd-income",
        "2026-08-01",
        income=10,
        income_instrument=2,
        income_account="cash-usd",
    )
    add_transaction(
        planning_db,
        "usd-expense",
        "2026-08-02",
        outcome=5,
        outcome_instrument=2,
        outcome_account="cash-usd",
    )

    result = get_cash_flow(
        planning_db, start_date="2026-08-01", end_date="2026-08-31"
    )

    assert result["income"] == 900
    assert result["operating_expenses"] == 450


def test_cash_flow_does_not_read_unrelated_reminder_rates(planning_db):
    add_marker(
        planning_db,
        "future-usd-payment",
        "9999-12-31",
        income=1,
        outcome=1,
        income_instrument=2,
        outcome_instrument=2,
        income_account="loan",
        outcome_account="cash-usd",
    )
    add_transaction(planning_db, "income", "2026-08-01", income=100)
    planning_db.connect().execute("UPDATE instruments SET rate=NULL WHERE id=2")

    result = get_cash_flow(
        planning_db, start_date="2026-08-01", end_date="2026-08-31"
    )

    assert result["income"] == 100


def test_cash_flow_has_null_savings_rate_without_income(planning_db):
    add_transaction(planning_db, "expense", "2026-08-02", outcome=200)

    result = get_cash_flow(
        planning_db, start_date="2026-08-01", end_date="2026-08-31"
    )

    assert result["savings_rate_before_debt_service_pct"] is None
    assert result["savings_rate_after_debt_service_pct"] is None


def test_last_complete_month_respects_user_budget_month(planning_db):
    planning_db.connect().execute("UPDATE users SET month_start_day=8")
    add_transaction(planning_db, "included", "2026-08-07", outcome=200)
    add_transaction(planning_db, "partial", "2026-08-08", outcome=500)

    result = get_cash_flow(
        planning_db, period="last_complete_month", as_of=date(2026, 8, 23)
    )

    assert result["period"] == {
        "preset": "last_complete_month",
        "start": "2026-07-08",
        "end": "2026-08-07",
        "complete": True,
    }
    assert result["operating_expenses"] == 200


def test_cash_flow_separates_debt_service_from_operating_expense(planning_db):
    add_transaction(
        planning_db,
        "payment",
        "2026-07-10",
        income=100_000,
        outcome=100_000,
        income_account="loan",
        outcome_account="cash-rub",
    )

    result = get_cash_flow(
        planning_db, start_date="2026-07-01", end_date="2026-07-31"
    )

    assert result["operating_expenses"] == 0
    assert result["debt_service_cash_outflow"] == 100_000
    assert result["net_cash_flow_after_debt_service"] == -100_000
    assert result["flow_components"]["debt_service_outflow"] == {
        "amount": 100_000,
        "count": 1,
    }


def test_cash_flow_does_not_count_borrowing_as_income(planning_db):
    add_transaction(
        planning_db,
        "borrowing",
        "2026-07-10",
        income=300_000,
        outcome=300_000,
        income_account="cash-rub",
        outcome_account="loan",
    )

    result = get_cash_flow(
        planning_db, start_date="2026-07-01", end_date="2026-07-31"
    )

    assert result["income"] == 0
    assert result["financing_inflow"] == 300_000
    assert result["net_cash_flow_after_debt_service"] == 300_000


def test_cash_flow_uses_financing_destination_side_without_source_rate(planning_db):
    planning_db.connect().execute(
        "INSERT INTO accounts(id,title,type,instrument,balance,in_balance,savings,archive,user,changed) "
        "VALUES ('loan-usd','USD Loan','loan',2,-10,1,0,0,1,1)"
    )
    add_transaction(
        planning_db,
        "borrowing",
        "2026-07-10",
        income=100,
        income_instrument=1,
        income_account="cash-rub",
        outcome=1,
        outcome_instrument=2,
        outcome_account="loan-usd",
    )
    planning_db.connect().execute("UPDATE instruments SET rate=NULL WHERE id=2")

    result = get_cash_flow(
        planning_db, start_date="2026-07-01", end_date="2026-07-31"
    )

    assert result["financing_inflow"] == 100


def test_liability_funded_spending_has_equal_expense_and_financing(planning_db):
    add_transaction(
        planning_db,
        "card-purchase",
        "2026-07-10",
        outcome=30_000,
        outcome_account="credit",
        tag="food",
    )

    result = get_cash_flow(
        planning_db, start_date="2026-07-01", end_date="2026-07-31"
    )

    assert result["operating_expenses"] == 30_000
    assert result["financing_inflow"] == 30_000
    assert result["operating_net_cash_flow"] == -30_000
    assert result["net_cash_flow_after_debt_service"] == 0


def test_cash_flow_bounds_structurally_unknown_transactions(planning_db):
    for index in range(51):
        add_transaction(
            planning_db,
            f"unknown-{index}",
            "2026-07-10",
            income=10,
            income_account="loan",
        )

    result = get_cash_flow(
        planning_db, start_date="2026-07-01", end_date="2026-07-31"
    )

    assert result["flow_components"]["unknown"] == {"amount": 510, "count": 51}
    assert len(result["uncertain_transactions"]) == 50
    assert result["uncertain_transactions"][0] == {
        "transaction_id": "unknown-0",
        "classification": "unknown",
        "classification_reason": "account_relationship_is_not_classifiable",
        "confidence": "low",
    }
    assert "unknown_transaction_flows_excluded" in result["data_quality"][
        "warnings"
    ]


@pytest.mark.parametrize("months", [3, 6, 12])
def test_spending_baseline_returns_requested_complete_windows(planning_db, months):
    add_transaction(planning_db, "partial", "2026-08-01", outcome=999)

    result = get_spending_baseline(
        planning_db, months=months, as_of=date(2026, 8, 23)
    )

    assert result["months_used"] == months
    assert len(result["monthly"]) == months
    assert result["latest_complete_month"] == 0


def test_spending_baseline_partial_month_is_excluded_from_statistics(planning_db):
    for tx_id, tx_date, amount in (
        ("may", "2026-05-10", 100),
        ("june", "2026-06-10", 200),
        ("july", "2026-07-10", 300),
        ("partial", "2026-08-10", 500),
    ):
        add_transaction(planning_db, tx_id, tx_date, outcome=amount)

    result = get_spending_baseline(
        planning_db,
        months=3,
        as_of=date(2026, 8, 28),
        include_current_partial_month=True,
    )

    assert result["monthly_series"][-1] == {
        "label": "2026-08",
        "month": "2026-08",
        "start": "2026-08-01",
        "end": "2026-08-28",
        "complete": False,
        "days_elapsed": 28,
        "days_total": 31,
        "outcome": 500,
    }
    assert result["monthly"] == result["monthly_series"]
    assert result["median"] == 200


def test_spending_baseline_partial_month_respects_custom_period_start(planning_db):
    planning_db.connect().execute("UPDATE users SET month_start_day=8")
    for tx_id, tx_date, amount in (
        ("may", "2026-05-10", 100),
        ("june", "2026-06-10", 200),
        ("july", "2026-07-10", 300),
        ("partial", "2026-08-10", 500),
    ):
        add_transaction(planning_db, tx_id, tx_date, outcome=amount)

    result = get_spending_baseline(
        planning_db,
        months=3,
        as_of=date(2026, 8, 28),
        include_current_partial_month=True,
    )

    assert result["monthly_series"][-1] == {
        "label": "2026-08",
        "month": "2026-08",
        "start": "2026-08-08",
        "end": "2026-08-28",
        "complete": False,
        "days_elapsed": 21,
        "days_total": 31,
        "outcome": 500,
    }
    assert result["median"] == 200


def test_spending_baseline_uses_ten_percent_trimmed_mean(planning_db):
    for month, amount in zip(
        range(10, 0, -1), [1, 1, 1, 1, 1, 1, 1, 1, 100, 1_000]
    ):
        add_transaction(
            planning_db, f"trim-{month}", f"2026-{month:02d}-10", outcome=amount
        )

    result = get_spending_baseline(
        planning_db, months=10, as_of=date(2026, 11, 28)
    )

    assert result["trimmed_mean"] == 13.38
    assert result["trimmed_mean_method"] == (
        "statistics.fmean after floor(10%) from each tail"
    )


def test_spending_baseline_classifies_expense_patterns(planning_db):
    planning_db.connect().execute(
        "INSERT INTO merchants(id,title,user,changed) VALUES ('net','Net Service',1,1)"
    )
    events = {
        "monthly": [("2026-05-01", 100), ("2026-06-01", 105), ("2026-07-01", 95)],
        "quarterly": [("2025-10-10", 200), ("2026-01-08", 200), ("2026-04-08", 200)],
        "semiannual": [("2025-02-01", 300), ("2025-08-01", 300), ("2026-02-01", 300)],
        "annual": [("2024-08-10", 400), ("2025-08-10", 400)],
        "one-off": [("2026-04-11", 500)],
        "unknown": [("2026-05-02", 100), ("2026-06-02", 140), ("2026-07-02", 100)],
    }
    for name, entries in events.items():
        for index, (tx_date, amount) in enumerate(entries):
            add_transaction(
                planning_db,
                f"{name}-{index}",
                tx_date,
                outcome=amount,
                tag="food",
                payee=name,
                merchant="net" if name == "monthly" else None,
                outcome_account="credit" if name == "one-off" else "cash-rub",
            )

    result = get_spending_baseline(
        planning_db, months=24, as_of=date(2026, 8, 28)
    )

    classes = {item["name"]: item["classification"] for item in result["expense_patterns"]}
    assert classes == {
        "Net Service": "recurring_monthly",
        "quarterly": "likely_quarterly",
        "semiannual": "likely_semiannual",
        "annual": "likely_annual",
        "one-off": "one_off",
        "unknown": "unknown",
    }
    assert result["pattern_summary"]["by_class"] == {
        "recurring_monthly": 1,
        "likely_quarterly": 1,
        "likely_semiannual": 1,
        "likely_annual": 1,
        "one_off": 1,
        "unknown": 1,
    }
    assert result["expense_patterns"][0]["total_amount"] == 900
    assert result["patterns_total"] == result["patterns_returned"] == 6
    assert result["patterns_truncated"] is False


def test_spending_baseline_limits_expense_patterns_to_top_hundred(planning_db):
    for index in range(101):
        add_transaction(
            planning_db,
            f"one-off-{index}",
            "2026-07-10",
            outcome=index + 1,
            tag="food",
            payee=f"Merchant {index}",
        )

    result = get_spending_baseline(
        planning_db, months=3, as_of=date(2026, 8, 28)
    )

    assert result["patterns_total"] == 101
    assert result["patterns_returned"] == len(result["expense_patterns"]) == 100
    assert result["patterns_truncated"] is True
    assert result["expense_patterns"][0]["total_amount"] == 101


def test_spending_baseline_patterns_include_null_income_expense(planning_db):
    add_transaction(
        planning_db,
        "null-income-expense",
        "2026-07-10",
        outcome=250,
        tag="food",
        payee="Nullable expense",
    )
    planning_db.connect().execute(
        "UPDATE transactions SET income=NULL WHERE id='null-income-expense'"
    )

    result = get_spending_baseline(
        planning_db, months=3, as_of=date(2026, 8, 28)
    )

    assert result["expense_patterns"] == [
        {
            "name": "Nullable expense",
            "normalized_name": "nullableexpense",
            "category_id": "food",
            "category": "Food",
            "classification": "one_off",
            "confidence": "low",
            "event_count": 1,
            "intervals_days": [],
            "average_amount": 250,
            "total_amount": 250,
        }
    ]


def test_spending_baseline_median_resists_outlier_and_includes_descendants(planning_db):
    for index, (month, amount) in enumerate(
        [(2, 100), (3, 100), (4, 100), (5, 10_000), (6, 100), (7, 200)]
    ):
        add_transaction(
            planning_db,
            f"expense-{index}",
            f"2026-{month:02d}-10",
            outcome=amount,
            tag="groceries",
        )
    add_transaction(planning_db, "partial", "2026-08-01", outcome=50_000, tag="food")

    result = get_spending_baseline(
        planning_db, months=6, category_id="food", as_of=date(2026, 8, 23)
    )

    assert [item["outcome"] for item in result["monthly"]] == [
        100,
        100,
        100,
        10_000,
        100,
        200,
    ]
    assert result["mean"] == 1_766.67
    assert result["median"] == 100
    assert result["p25"] == 100
    assert result["p75"] == 175
    assert result["latest_vs_median_pct"] == 100
    assert result["percentile_method"] == "statistics.quantiles inclusive"


def test_spending_baseline_converts_multiple_currencies(planning_db):
    add_transaction(
        planning_db,
        "usd-expense",
        "2026-07-10",
        outcome=10,
        outcome_instrument=2,
        outcome_account="cash-usd",
    )

    result = get_spending_baseline(
        planning_db, months=3, as_of=date(2026, 8, 23)
    )

    assert result["latest_complete_month"] == 900


def test_spending_baseline_category_filter_excludes_other_categories(planning_db):
    add_transaction(
        planning_db, "essential", "2026-07-10", outcome=100, tag="groceries"
    )
    add_transaction(
        planning_db, "other", "2026-07-11", outcome=900, tag="transport"
    )

    result = get_spending_baseline(
        planning_db, months=3, category_id="food", as_of=date(2026, 8, 23)
    )

    assert result["latest_complete_month"] == 100


def test_compare_periods_reports_increases_decreases_and_new_categories(planning_db):
    add_transaction(planning_db, "june-income", "2026-06-10", income=1_000, tag="salary")
    add_transaction(planning_db, "june-food", "2026-06-11", outcome=300, tag="food")
    add_transaction(planning_db, "july-income", "2026-07-10", income=1_200, tag="salary")
    add_transaction(planning_db, "july-food", "2026-07-11", outcome=200, tag="food")
    add_transaction(
        planning_db, "july-transport", "2026-07-12", outcome=50, tag="transport"
    )

    result = compare_periods(
        planning_db, preset="last_month_vs_previous", as_of=date(2026, 8, 23)
    )

    assert result["income"] == {
        "period_a": 1_000,
        "period_b": 1_200,
        "delta": 200,
        "delta_pct": 20,
    }
    assert result["outcome"]["delta"] == -50
    transport = next(
        item for item in result["category_deltas"] if item["category_id"] == "transport"
    )
    assert transport["period_a"] == 0
    assert transport["period_b"] == 50
    assert transport["delta_pct"] is None


def test_compare_periods_accepts_arbitrary_ranges(planning_db):
    add_transaction(planning_db, "a", "2026-01-01", outcome=100)
    add_transaction(planning_db, "b", "2026-02-01", outcome=150)

    result = compare_periods(
        planning_db,
        period_a={"start_date": "2026-01-01", "end_date": "2026-01-31"},
        period_b={"start_date": "2026-02-01", "end_date": "2026-02-28"},
    )

    assert result["outcome"]["delta_pct"] == 50


def test_custom_comparison_completeness_uses_injected_as_of(planning_db):
    result = compare_periods(
        planning_db,
        period_a={"start_date": "2027-01-01", "end_date": "2027-01-31"},
        period_b={"start_date": "2027-02-01", "end_date": "2027-02-28"},
        as_of=date(2027, 3, 1),
    )

    assert result["period_a"]["complete"] is True
    assert result["period_b"]["complete"] is True


def test_compare_last_complete_month_with_year_ago(planning_db):
    add_transaction(planning_db, "old", "2025-07-10", outcome=100)
    add_transaction(planning_db, "new", "2026-07-10", outcome=125)

    result = compare_periods(
        planning_db,
        preset="last_complete_month_vs_year_ago",
        as_of=date(2026, 8, 23),
    )

    assert result["period_a"]["start"] == "2025-07-01"
    assert result["period_b"]["start"] == "2026-07-01"
    assert result["outcome"]["delta_pct"] == 25


def test_emergency_fund_requires_explicit_essential_configuration(planning_db):
    result = get_emergency_fund_status(planning_db)

    assert result["status"] == "configuration_required"
    assert result["missing"] == [
        "essential_category_ids or monthly_essential_override"
    ]
    assert result["data_quality"]["missing_exchange_rates"] == []


def test_emergency_fund_uses_descendant_category_median(planning_db):
    for month, amount in [(5, 100), (6, 200), (7, 300)]:
        add_transaction(
            planning_db,
            f"essential-{month}",
            f"2026-{month:02d}-10",
            outcome=amount,
            tag="groceries",
        )

    result = get_emergency_fund_status(
        planning_db,
        essential_category_ids=["food"],
        baseline_months=3,
        target_months=6,
        as_of=date(2026, 8, 23),
    )

    assert result["monthly_essential_baseline"] == 200
    assert result["coverage_months"] == 120


@pytest.mark.parametrize(
    ("monthly", "expected_status", "expected_gap"),
    [(4_000, "at_target", 0), (3_000, "above_target", -6_000), (5_000, "below_target", 6_000)],
)
def test_emergency_fund_override_excludes_credit_and_restricted_deposit(
    planning_db, monthly, expected_status, expected_gap
):
    result = get_emergency_fund_status(
        planning_db, monthly_essential_override=monthly, target_months=6
    )

    assert result["reserve"] == {
        "own_liquid_funds": 19_000,
        "accessible_savings": 5_000,
        "total_eligible": 24_000,
    }
    assert result["target_amount"] == monthly * 6
    assert result["gap"] == expected_gap
    assert result["status"] == expected_status


def test_emergency_fund_zero_baseline_has_undefined_coverage(planning_db):
    result = get_emergency_fund_status(
        planning_db, monthly_essential_override=0, target_months=6
    )

    assert result["coverage_months"] is None
    assert result["status"] == "configuration_required"


def test_emergency_fund_rejects_two_baseline_sources(planning_db):
    with pytest.raises(InputValidationError):
        get_emergency_fund_status(
            planning_db,
            essential_category_ids=["food"],
            monthly_essential_override=100,
        )


def test_debt_service_counts_transfer_into_debt_account(planning_db):
    add_transaction(planning_db, "salary", "2026-07-01", income=1_000)
    add_transaction(
        planning_db,
        "payment",
        "2026-07-10",
        income=500,
        outcome=500,
        income_account="loan",
        outcome_account="cash-rub",
    )
    add_transaction(
        planning_db,
        "borrowing",
        "2026-07-11",
        income=200,
        outcome=200,
        income_account="cash-rub",
        outcome_account="loan",
    )

    result = get_debt_service(planning_db, as_of=date(2026, 8, 23))

    assert result["total_liabilities"] == 7_000
    assert {item["account_id"] for item in result["obligations"]} == {
        "credit",
        "loan",
    }
    assert result["last_complete_month"] == {
        "operating_income": 1_000,
        "debt_service_cash_outflow": 500,
        "debt_service_ratio_pct": 50,
    }
    assert result["trailing_3_complete_months"] == {
        "average_debt_service_cash_outflow": 166.67,
    }
    assert "current_debt_balance" not in result
    assert "accounts" not in result
    assert "trailing_3_month_average_payment" not in result


def test_debt_service_handles_multiple_currencies_and_zero_income(planning_db):
    planning_db.connect().execute(
        "INSERT INTO accounts(id,title,type,instrument,balance,in_balance,savings,archive,user,changed) "
        "VALUES ('loan-usd','USD Loan','loan',2,-10,1,0,0,1,1)"
    )
    add_transaction(
        planning_db,
        "usd-payment",
        "2026-07-10",
        income=1,
        income_instrument=2,
        income_account="loan-usd",
        outcome=90,
        outcome_account="cash-rub",
    )

    result = get_debt_service(planning_db, as_of=date(2026, 8, 23))

    assert result["total_liabilities"] == 7_900
    assert result["last_complete_month"]["debt_service_cash_outflow"] == 90
    assert result["last_complete_month"]["debt_service_ratio_pct"] is None
    assert len(result["obligations"]) == 3


def test_debt_service_with_no_debt_is_zero(planning_db):
    planning_db.connect().execute("UPDATE accounts SET balance=0 WHERE balance<0")

    result = get_debt_service(planning_db, as_of=date(2026, 8, 23))

    assert result["total_liabilities"] == 0
    assert result["obligations"] == []
    assert result["last_complete_month"]["debt_service_cash_outflow"] == 0


def test_debt_service_excludes_transfer_into_positive_debt_account(planning_db):
    planning_db.connect().execute(
        "INSERT INTO accounts(id,title,type,instrument,balance,in_balance,savings,archive,user,changed) "
        "VALUES ('loan-positive','Positive loan','loan',1,100,1,0,0,1,1)"
    )
    add_transaction(
        planning_db,
        "not-a-payment",
        "2026-07-10",
        income=500,
        outcome=500,
        income_account="loan-positive",
        outcome_account="cash-rub",
    )

    result = get_debt_service(planning_db, as_of=date(2026, 8, 23))

    assert result["last_complete_month"]["debt_service_cash_outflow"] == 0


def test_debt_service_uses_actual_source_cash_outflow(planning_db):
    planning_db.connect().execute(
        "INSERT INTO accounts(id,title,type,instrument,balance,in_balance,savings,archive,user,changed) "
        "VALUES ('loan-usd','USD Loan','loan',2,-10,1,0,0,1,1)"
    )
    add_transaction(
        planning_db,
        "fx-payment",
        "2026-07-10",
        income=1,
        income_instrument=2,
        income_account="loan-usd",
        outcome=100,
        outcome_instrument=1,
        outcome_account="cash-rub",
    )

    result = get_debt_service(planning_db, as_of=date(2026, 8, 23))

    assert result["last_complete_month"]["debt_service_cash_outflow"] == 100


def test_forecast_sums_planned_income_and_outcome(planning_db):
    add_marker(planning_db, "income", "2026-09-01", income=1_000, payee="Salary")
    add_marker(planning_db, "outcome", "2026-09-02", outcome=200, payee="Rent")

    result = forecast_cash_flow(
        planning_db, horizon_days=30, as_of=date(2026, 8, 23)
    )

    assert result["starting_liquid_funds"] == 24_000
    assert result["scheduled"] == {"income": 1_000, "outcome": 200, "net": 800}
    assert result["scenarios"]["scheduled_only"]["ending_liquid_funds"] == 24_800


@pytest.mark.parametrize(
    ("horizon", "expected"), [(30, 100), (60, 300), (90, 600)]
)
def test_forecast_horizons_exclude_later_markers(planning_db, horizon, expected):
    add_marker(planning_db, "m30", "2026-09-01", outcome=100)
    add_marker(planning_db, "m60", "2026-10-01", outcome=200)
    add_marker(planning_db, "m90", "2026-11-01", outcome=300)
    add_marker(planning_db, "outside", "2026-12-01", outcome=1_000)

    result = forecast_cash_flow(
        planning_db, horizon_days=horizon, as_of=date(2026, 8, 23)
    )

    assert result["scheduled"]["outcome"] == expected


def test_forecast_30_days_has_exclusive_day_30_upper_bound(planning_db):
    add_marker(planning_db, "day-29", "2026-09-21", outcome=100)
    add_marker(planning_db, "day-30", "2026-09-22", outcome=200)

    result = forecast_cash_flow(
        planning_db, horizon_days=30, as_of=date(2026, 8, 23)
    )

    assert result["scheduled"]["outcome"] == 100


def test_forecast_deduplicates_detected_recurring_against_marker(planning_db):
    for month in range(2, 8):
        add_transaction(
            planning_db,
            f"netflix-{month}",
            f"2026-{month:02d}-10",
            outcome=100,
            payee="Netflix",
        )
        add_transaction(
            planning_db,
            f"gym-{month}",
            f"2026-{month:02d}-15",
            outcome=50,
            payee="Gym",
        )
    add_marker(planning_db, "netflix", "2026-09-10", outcome=100, payee="Netflix")

    result = forecast_cash_flow(
        planning_db, horizon_days=30, as_of=date(2026, 8, 23)
    )

    assert result["detected_recurring"] == {"outcome": 50, "confidence": "medium"}
    assert result["scenarios"]["scheduled_plus_recurring"]["ending_liquid_funds"] == 23_850
    assert result["baseline_discretionary_spend"]["monthly_median"] == 150
    assert result["scenarios"]["baseline_spending"]["ending_liquid_funds"] == 23_850


def test_forecast_without_plans_returns_warning(planning_db):
    result = forecast_cash_flow(
        planning_db, horizon_days=30, as_of=date(2026, 8, 23)
    )

    assert result["scheduled"] == {"income": 0, "outcome": 0, "net": 0}
    assert "no planned reminder markers in horizon" in result["warnings"]


def test_forecast_uses_account_currency_when_marker_currency_is_missing(planning_db):
    add_marker(
        planning_db,
        "usd",
        "2026-09-01",
        outcome=10,
        outcome_account="cash-usd",
    )

    result = forecast_cash_flow(
        planning_db, horizon_days=30, as_of=date(2026, 8, 23)
    )

    assert result["scheduled"]["outcome"] == 900


def test_forecast_rejects_missing_marker_exchange_rate(planning_db):
    add_marker(
        planning_db,
        "usd",
        "2026-09-01",
        outcome=10,
        outcome_instrument=2,
        outcome_account="cash-usd",
    )
    planning_db.connect().execute("UPDATE instruments SET rate=NULL WHERE id=2")

    with pytest.raises(CurrencyRateError):
        forecast_cash_flow(planning_db, horizon_days=30, as_of=date(2026, 8, 23))


def test_forecast_rejects_unsupported_horizon(planning_db):
    with pytest.raises(InputValidationError):
        forecast_cash_flow(planning_db, horizon_days=45)


def test_financial_snapshot_composes_primitives_and_complete_periods(planning_db):
    add_transaction(planning_db, "july-income", "2026-07-10", income=1_000)
    add_transaction(planning_db, "july-expense", "2026-07-11", outcome=200)
    add_transaction(planning_db, "partial", "2026-08-10", outcome=9_999)
    add_marker(planning_db, "planned", "2026-09-01", outcome=300)

    result = get_financial_snapshot(planning_db, as_of=date(2026, 8, 23))

    assert result["as_of"] == "2026-08-23"
    assert result["currency"] == "RUB"
    assert result["net_worth"] == 37_000
    assert result["own_liquid_funds"] == 19_000
    assert result["accessible_savings"] == 5_000
    assert result["restricted_savings"] == 20_000
    assert result["cash_flow"]["last_complete_month"]["outcome"] == 200
    assert result["cash_flow"]["trailing_3_months_average"]["outcome"] == 66.67
    assert result["cash_flow"]["trailing_12_months_average"]["outcome"] == 16.67
    assert result["upcoming_30_days"]["planned_outcome"] == 300


def test_financial_snapshot_reports_personal_debt_position(planning_db):
    planning_db.connect().execute(
        "INSERT INTO accounts(id,title,type,instrument,balance,in_balance,savings,archive,user,changed) "
        "VALUES ('personal-debt','Personal debt','debt',1,-500,1,0,0,1,1)"
    )

    result = get_financial_snapshot(planning_db, as_of=date(2026, 8, 23))

    assert result["debt_position"] == {"owed_to_you": 0, "you_owe": 500, "net": -500}


def test_financial_position_unifies_assets_liabilities_and_monthly_flow(planning_db):
    planning_db.connect().executemany(
        "INSERT INTO accounts(id,title,type,instrument,balance,in_balance,savings,archive,user,changed) "
        "VALUES (?,?,?,?,?,?,0,0,1,1)",
        [
            ("installment", "Installment", "checking", 1, -300, 0),
            ("personal", "Personal debt", "debt", 1, -200, 0),
        ],
    )
    for month in (5, 6, 7):
        add_transaction(
            planning_db, f"income-{month}", f"2026-{month:02d}-10", income=1_000
        )
        add_transaction(
            planning_db, f"expense-{month}", f"2026-{month:02d}-11", outcome=200
        )
        add_transaction(
            planning_db,
            f"debt-{month}",
            f"2026-{month:02d}-12",
            income=100,
            outcome=100,
            income_account="loan",
            outcome_account="cash-rub",
        )

    result = planning.get_financial_position(
        planning_db,
        {"installment": {"classification": "installment"}},
        as_of=date(2026, 8, 23),
    )

    assert result == {
        "as_of": "2026-08-23",
        "currency": "RUB",
        "liquid_assets": 26_000,
        "restricted_assets": 20_000,
        "total_assets": 46_000,
        "loans": 6_000,
        "credit_cards": 1_000,
        "installments": 300,
        "personal_debts": 200,
        "total_liabilities": 7_500,
        "net_worth": 38_500,
        "operating_monthly_income": 1_000,
        "operating_monthly_expenses": 200,
        "monthly_debt_service": 100,
        "free_cash_flow_after_debt_service": 700,
        "monthly_basis": "trailing_3_complete_months_average",
        "in_balance_semantics": "reported_by_zenmoney_but_not_used_for_economic_position",
        "data_quality": {
            "last_sync": None,
            "staleness": "never_synced",
            "complete_months_available": 3,
            "missing_exchange_rates": [],
            "warnings": [],
        },
    }


def test_financial_position_rounds_buckets_before_reconciling_totals(planning_db):
    planning_db.connect().execute("UPDATE accounts SET balance=0")
    planning_db.connect().execute(
        "INSERT INTO instruments(id,title,short_title,symbol,rate,changed) "
        "VALUES (3,'Small unit','SMU','s',0.004,1)"
    )
    planning_db.connect().executemany(
        "INSERT INTO accounts(id,title,type,instrument,balance,in_balance,savings,archive,user,changed) "
        "VALUES (?,?,?,?,1,1,0,0,1,1)",
        [
            ("tiny-liquid", "Tiny liquid", "checking", 3),
            ("tiny-restricted", "Tiny restricted", "deposit", 3),
        ],
    )

    result = planning.get_financial_position(
        planning_db, as_of=date(2026, 8, 23)
    )

    assert result["liquid_assets"] == 0
    assert result["restricted_assets"] == 0
    assert result["total_assets"] == 0
    assert result["net_worth"] == 0


def test_financial_position_aggregates_liabilities_before_rounding(planning_db):
    planning_db.connect().execute("UPDATE accounts SET balance=0")
    planning_db.connect().execute(
        "INSERT INTO instruments(id,title,short_title,symbol,rate,changed) "
        "VALUES (3,'Small unit','SMU','s',0.004,1)"
    )
    planning_db.connect().executemany(
        "INSERT INTO accounts(id,title,type,instrument,balance,in_balance,savings,archive,user,changed) "
        "VALUES (?,?,'loan',3,-1,0,0,0,1,1)",
        [("tiny-loan-1", "Tiny loan 1"), ("tiny-loan-2", "Tiny loan 2")],
    )

    result = planning.get_financial_position(
        planning_db, as_of=date(2026, 8, 23)
    )

    assert result["loans"] == 0.01
    assert result["total_liabilities"] == 0.01
    assert result["net_worth"] == -0.01


def test_financial_position_rounds_average_flow_half_up_once(planning_db):
    for month in (5, 6, 7):
        add_transaction(
            planning_db,
            f"half-cent-{month}",
            f"2026-{month:02d}-10",
            income=1.005,
        )

    result = planning.get_financial_position(
        planning_db, as_of=date(2026, 8, 23)
    )

    assert result["operating_monthly_income"] == 1.01
    assert result["free_cash_flow_after_debt_service"] == 1.01


def test_financial_snapshot_fails_explicitly_for_empty_cache():
    db = HardenedDatabase(":memory:")
    db.init_schema()

    with pytest.raises(FinancialDataError):
        get_financial_snapshot(db, as_of=date(2026, 8, 23))


def test_cash_flow_scans_50_000_transactions_without_truncation(planning_db):
    planning_db.connect().executemany(
        """INSERT INTO transactions(
            id,date,user,deleted,hold,income,income_instrument,income_account,
            outcome,outcome_instrument,outcome_account,changed
        ) VALUES (?, '2026-07-01', 1, 0, 0, 0, 1, 'cash-rub', 1, 1, 'cash-rub', 1)""",
        [(f"bulk-{index}",) for index in range(50_000)],
    )

    result = get_cash_flow(
        planning_db, start_date="2026-07-01", end_date="2026-07-31"
    )

    assert result["operating_expenses"] == 50_000
    assert result["flow_components"]["operating_expense"]["count"] == 50_000
    assert result["data_quality"]["complete_months_available"] == 1
