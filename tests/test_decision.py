from datetime import date

import pytest

from zenmoney_mcp.decision import (
    build_financial_plan,
    compare_debt_strategies,
    plan_debt_payoff,
    plan_emergency_fund,
    plan_financial_goal,
    plan_multiple_goals,
    run_financial_scenario,
)
from zenmoney_mcp.hardened_database import HardenedDatabase
from zenmoney_mcp.validation import InputValidationError


@pytest.fixture
def decision_db() -> HardenedDatabase:
    db = HardenedDatabase(":memory:")
    db.init_schema()
    conn = db.connect()
    conn.execute(
        "INSERT INTO instruments(id,title,short_title,symbol,rate,changed) "
        "VALUES (1,'Ruble','RUB','₽',1,1)"
    )
    conn.execute(
        "INSERT INTO users(id,login,currency,parent,month_start_day,changed) "
        "VALUES (1,'u',1,NULL,1,1)"
    )
    conn.executemany(
        """INSERT INTO accounts(
               id,title,type,instrument,balance,credit_limit,in_balance,
               savings,archive,user,changed
           ) VALUES (?,?,?,?,?,?,?,?,?,1,1)""",
        [
            ("cash", "Cash", "checking", 1, 120_000, None, 1, 0, 0),
            ("savings", "Savings", "checking", 1, 180_000, None, 1, 1, 0),
            ("deposit", "Deposit", "deposit", 1, 500_000, None, 1, 1, 0),
            ("credit", "Credit", "ccard", 1, -10_000, 100_000, 1, 0, 0),
            ("loan", "Loan", "loan", 1, -1_000, None, 1, 0, 0),
        ],
    )
    conn.executemany(
        "INSERT INTO tags(id,title,parent,show_income,show_outcome,user,changed) "
        "VALUES (?,?,?,?,?,1,1)",
        [
            ("essential", "Essential", None, 0, 1),
            ("food", "Food", "essential", 0, 1),
            ("other", "Other", None, 0, 1),
            ("salary", "Salary", None, 1, 0),
        ],
    )
    for month in (2, 3, 4, 5, 6, 7):
        conn.executemany(
            """INSERT INTO transactions(
                   id,date,user,deleted,hold,income,income_instrument,income_account,
                   outcome,outcome_instrument,outcome_account,tag,changed
               ) VALUES (?,?,1,0,0,?,1,'cash',?,1,'cash',?,1)""",
            [
                (f"income-{month}", f"2026-{month:02d}-10", 200_000, 0, '["salary"]'),
                (f"essential-{month}", f"2026-{month:02d}-11", 0, 120_000, '["food"]'),
                (f"other-{month}", f"2026-{month:02d}-12", 0, 30_000, '["other"]'),
            ],
        )
    conn.commit()
    return db


def test_emergency_plan_uses_phase2_facts_and_excludes_credit_and_deposit(decision_db):
    result = plan_emergency_fund(
        decision_db,
        target_months=6,
        essential_category_ids=["essential"],
        minimum_liquidity_buffer=100_000,
        allocation_pct_of_free_cash_flow=75,
        as_of=date(2026, 8, 23),
    )

    assert result["currency"] == "RUB"
    assert result["current"] == {"eligible_reserve": 300_000.0, "coverage_months": 2.5}
    assert result["target"] == {"months": 6, "amount": 720_000.0, "gap": 420_000.0}
    assert result["capacity"] == {
        "monthly_free_cash_flow": 50_000.0,
        "allocation_pct": 75.0,
        "monthly_contribution": 37_500.0,
    }
    assert result["plan"] == {
        "status": "building",
        "estimated_months_to_target": 12,
        "estimated_completion_date": "2027-08-31",
    }
    assert result["constraints"]["credit_included"] is False
    assert result["constraints"]["restricted_deposits_included"] is False


def test_emergency_plan_can_explicitly_include_restricted_deposits(decision_db):
    result = plan_emergency_fund(
        decision_db,
        target_months=6,
        monthly_essential_override=120_000,
        include_restricted_deposits=True,
        as_of=date(2026, 8, 23),
    )

    assert result["current"]["eligible_reserve"] == 800_000
    assert result["target"]["gap"] == 0
    assert result["plan"]["status"] == "funded"
    assert result["plan"]["estimated_months_to_target"] == 0


def test_emergency_plan_reports_zero_free_cash_flow_without_dividing(decision_db):
    decision_db.connect().execute(
        "UPDATE transactions SET income=150000 WHERE id LIKE 'income-%'"
    )

    result = plan_emergency_fund(
        decision_db,
        target_months=6,
        monthly_essential_override=120_000,
        as_of=date(2026, 8, 23),
    )

    assert result["capacity"]["monthly_free_cash_flow"] == 0
    assert result["capacity"]["monthly_contribution"] == 0
    assert result["plan"] == {
        "status": "insufficient_capacity",
        "estimated_months_to_target": None,
        "estimated_completion_date": None,
    }


def test_emergency_plan_caps_allocation_and_reports_liquidity_gap(decision_db):
    result = plan_emergency_fund(
        decision_db,
        target_months=6,
        monthly_essential_override=120_000,
        minimum_liquidity_buffer=200_000,
        allocation_pct_of_free_cash_flow=100,
        as_of=date(2026, 8, 23),
    )

    assert result["capacity"]["monthly_contribution"] == 50_000
    assert result["constraints"]["minimum_liquidity_buffer"] == 200_000
    assert result["constraints"]["liquidity_buffer_gap"] == 80_000
    assert result["reasons"] == [
        {"metric": "emergency_fund_months", "actual": 2.5, "target": 6.0},
        {"metric": "liquid_own", "actual": 120_000.0, "target": 200_000.0},
    ]


def test_emergency_plan_returns_structured_configuration_gap(decision_db):
    result = plan_emergency_fund(decision_db, as_of=date(2026, 8, 23))

    assert result["status"] == "configuration_required"
    assert result["missing"] == [
        {
            "field": "essential_category_ids or monthly_essential_override",
            "reason": "Required to calculate essential monthly spending",
        }
    ]


@pytest.mark.parametrize("allocation", [-1, 101])
def test_emergency_plan_validates_allocation_percentage(decision_db, allocation):
    with pytest.raises(InputValidationError):
        plan_emergency_fund(
            decision_db,
            monthly_essential_override=100,
            allocation_pct_of_free_cash_flow=allocation,
        )


def add_debt(decision_db, account_id, balance):
    decision_db.connect().execute(
        """INSERT INTO accounts(
               id,title,type,instrument,balance,in_balance,savings,archive,user,changed
           ) VALUES (?,?, 'loan',1,?,1,0,0,1,1)""",
        (account_id, account_id, -balance),
    )


def test_debt_payoff_zero_apr_uses_exact_final_payment(decision_db):
    decision_db.connect().execute("UPDATE accounts SET balance=-100 WHERE id='loan'")

    result = plan_debt_payoff(
        decision_db,
        strategy="minimum_only",
        debt_accounts={"loan": {"apr_pct": 0, "minimum_payment": 30}},
        as_of=date(2026, 8, 23),
    )

    assert result["starting_debt"] == 100
    assert result["estimated_payoff_months"] == 4
    assert result["estimated_interest"] == 0
    assert [month["accounts"][0]["payment"] for month in result["schedule"]] == [
        30,
        30,
        30,
        10,
    ]
    assert result["schedule"][-1]["ending_debt"] == 0


def test_debt_payoff_applies_monthly_interest_before_payment(decision_db):
    decision_db.connect().execute("UPDATE accounts SET balance=-1000 WHERE id='loan'")

    result = plan_debt_payoff(
        decision_db,
        strategy="minimum_only",
        debt_accounts={"loan": {"apr_pct": 12, "minimum_payment": 100}},
        as_of=date(2026, 8, 23),
    )

    first = result["schedule"][0]["accounts"][0]
    assert first == {
        "account_id": "loan",
        "opening_balance": 1000,
        "interest": 10,
        "payment": 100,
        "principal": 90,
        "ending_balance": 910,
    }
    assert first["payment"] == first["interest"] + first["principal"]


def test_avalanche_targets_highest_apr_after_minimums(decision_db):
    decision_db.connect().execute("UPDATE accounts SET balance=-500 WHERE id='loan'")
    add_debt(decision_db, "low", 500)

    result = plan_debt_payoff(
        decision_db,
        monthly_extra_payment=100,
        strategy="avalanche",
        debt_accounts={
            "loan": {"apr_pct": 24, "minimum_payment": 50},
            "low": {"apr_pct": 12, "minimum_payment": 50},
        },
        as_of=date(2026, 8, 23),
    )

    first = {item["account_id"]: item for item in result["schedule"][0]["accounts"]}
    assert first["loan"]["payment"] == 150
    assert first["low"]["payment"] == 50


def test_snowball_targets_smallest_balance(decision_db):
    decision_db.connect().execute("UPDATE accounts SET balance=-500 WHERE id='loan'")
    add_debt(decision_db, "small", 300)

    result = plan_debt_payoff(
        decision_db,
        monthly_extra_payment=100,
        strategy="snowball",
        debt_accounts={
            "loan": {"apr_pct": 24, "minimum_payment": 50},
            "small": {"apr_pct": 12, "minimum_payment": 50},
        },
        as_of=date(2026, 8, 23),
    )

    first = {item["account_id"]: item for item in result["schedule"][0]["accounts"]}
    assert first["small"]["payment"] == 150
    assert first["loan"]["payment"] == 50


def test_avalanche_same_apr_uses_smaller_balance_then_id(decision_db):
    decision_db.connect().execute("UPDATE accounts SET balance=-500 WHERE id='loan'")
    add_debt(decision_db, "small", 300)

    result = plan_debt_payoff(
        decision_db,
        monthly_extra_payment=100,
        strategy="avalanche",
        debt_accounts={
            "loan": {"apr_pct": 10, "minimum_payment": 50},
            "small": {"apr_pct": 10, "minimum_payment": 50},
        },
        as_of=date(2026, 8, 23),
    )

    first = {item["account_id"]: item for item in result["schedule"][0]["accounts"]}
    assert first["small"]["payment"] == 150


def test_debt_payoff_reports_negative_amortization(decision_db):
    decision_db.connect().execute("UPDATE accounts SET balance=-1000 WHERE id='loan'")

    result = plan_debt_payoff(
        decision_db,
        strategy="minimum_only",
        debt_accounts={"loan": {"apr_pct": 120, "minimum_payment": 50}},
        as_of=date(2026, 8, 23),
    )

    assert result["estimated_payoff_months"] is None
    assert "negative_amortization" in result["warnings"]
    assert result["schedule"][0]["accounts"][0]["ending_balance"] == 1050


def test_debt_payoff_returns_configuration_required_for_missing_apr(decision_db):
    result = plan_debt_payoff(
        decision_db,
        strategy="avalanche",
        debt_accounts={"loan": {"minimum_payment": 50}},
    )

    assert result == {
        "status": "configuration_required",
        "missing": [
            {
                "field": "debt_accounts.loan.apr_pct",
                "reason": "Required to calculate interest and avalanche priority",
            }
        ],
    }


def test_debt_payoff_with_no_debt_is_complete(decision_db):
    decision_db.connect().execute("UPDATE accounts SET balance=0 WHERE type IN ('loan','debt')")

    result = plan_debt_payoff(decision_db, strategy="minimum_only", debt_accounts={})

    assert result["starting_debt"] == 0
    assert result["estimated_payoff_months"] == 0
    assert result["estimated_interest"] == 0
    assert result["schedule"] == []


def test_custom_debt_strategy_requires_complete_order(decision_db):
    result = plan_debt_payoff(
        decision_db,
        strategy="custom",
        debt_accounts={"loan": {"apr_pct": 0, "minimum_payment": 50}},
    )

    assert result["status"] == "configuration_required"
    assert result["missing"][0]["field"] == "custom_order"


def test_compare_debt_strategies_names_each_winning_criterion(decision_db):
    decision_db.connect().execute("UPDATE accounts SET balance=-500 WHERE id='loan'")
    add_debt(decision_db, "low", 500)

    result = compare_debt_strategies(
        decision_db,
        monthly_extra_payment=100,
        debt_accounts={
            "loan": {"apr_pct": 24, "minimum_payment": 50},
            "low": {"apr_pct": 12, "minimum_payment": 50},
        },
        as_of=date(2026, 8, 23),
    )

    assert [item["strategy"] for item in result["strategies"]] == [
        "minimum_only",
        "snowball",
        "avalanche",
    ]
    assert result["best_by_interest"] == "avalanche"
    assert result["best_by_duration"] in {"snowball", "avalanche"}
    assert "best" not in result


def test_compare_debt_strategies_has_no_winner_without_debt(decision_db):
    decision_db.connect().execute("UPDATE accounts SET balance=0 WHERE type IN ('loan','debt')")

    result = compare_debt_strategies(
        decision_db,
        monthly_extra_payment=100,
        debt_accounts={},
    )

    assert result["best_by_interest"] is None
    assert result["best_by_duration"] is None


def test_goal_with_deadline_calculates_required_monthly_contribution(decision_db):
    result = plan_financial_goal(
        decision_db,
        name="Course",
        target_amount=1_200,
        current_amount=0,
        target_date="2026-12-31",
        priority="high",
        as_of=date(2026, 8, 23),
    )

    assert result["goal"] == {
        "name": "Course",
        "target": 1_200,
        "current": 0,
        "gap": 1_200,
        "priority": "high",
    }
    assert result["required_monthly_contribution"] == 300
    assert result["available_free_cash_flow"] == 50_000
    assert result["feasibility"] == "feasible"
    assert result["margin"] == 49_700
    assert result["contribution_months"] == 4


def test_goal_with_contribution_calculates_completion_month_end(decision_db):
    result = plan_financial_goal(
        decision_db,
        name="Course",
        target_amount=1_200,
        current_amount=0,
        monthly_contribution=300,
        as_of=date(2026, 8, 23),
    )

    assert result["monthly_contribution"] == 300
    assert result["estimated_completion_date"] == "2026-12-31"
    assert result["estimated_completion_months"] == 4
    assert result["feasibility"] == "feasible"


@pytest.mark.parametrize("current", [1_200, 1_500])
def test_goal_already_funded_never_returns_a_negative_gap(decision_db, current):
    result = plan_financial_goal(
        decision_db,
        name="Funded",
        target_amount=1_200,
        current_amount=current,
        target_date="2026-12-31",
        as_of=date(2026, 8, 23),
    )

    assert result["goal"]["gap"] == 0
    assert result["required_monthly_contribution"] == 0
    assert result["feasibility"] == "funded"


def test_goal_rejects_deadline_before_first_future_month_end(decision_db):
    result = plan_financial_goal(
        decision_db,
        name="Immediate",
        target_amount=1_000,
        target_date="2026-08-31",
        as_of=date(2026, 8, 23),
    )

    assert result["feasibility"] == "infeasible"
    assert result["required_monthly_contribution"] is None
    assert result["reasons"] == [
        {
            "metric": "contribution_months",
            "actual": 0,
            "target": 1,
            "reason": "No future month-end contribution occurs by the deadline",
        }
    ]


def test_goal_reports_infeasible_when_free_cash_flow_is_zero(decision_db):
    decision_db.connect().execute(
        "UPDATE transactions SET income=150000 WHERE id LIKE 'income-%'"
    )

    result = plan_financial_goal(
        decision_db,
        name="Goal",
        target_amount=1_200,
        target_date="2026-12-31",
        as_of=date(2026, 8, 23),
    )

    assert result["available_free_cash_flow"] == 0
    assert result["feasibility"] == "infeasible"
    assert result["margin"] == -300


def test_goal_validates_mode_date_and_zero_return(decision_db):
    with pytest.raises(InputValidationError):
        plan_financial_goal(
            decision_db,
            name="Goal",
            target_amount=1_000,
            target_date="2026-13-01",
        )
    with pytest.raises(InputValidationError):
        plan_financial_goal(
            decision_db,
            name="Goal",
            target_amount=1_000,
            target_date="2026-12-31",
            monthly_contribution=100,
        )
    with pytest.raises(InputValidationError):
        plan_financial_goal(
            decision_db,
            name="Goal",
            target_amount=1_000,
            target_date="2026-12-31",
            annual_return_pct=1,
        )
    with pytest.raises(InputValidationError):
        plan_financial_goal(
            decision_db,
            name="Goal",
            target_amount=1_000,
            target_date="2026-12-31",
            priority="urgent",
        )


def test_multiple_goals_allocates_sufficient_cash_in_strict_priority_order():
    result = plan_multiple_goals(
        monthly_available=500,
        goals=[
            {
                "name": "First",
                "target_amount": 1_200,
                "current_amount": 0,
                "target_date": "2026-12-31",
                "priority": 1,
            },
            {
                "name": "Second",
                "target_amount": 800,
                "current_amount": 0,
                "target_date": "2026-12-31",
                "priority": 2,
            },
        ],
        as_of=date(2026, 8, 23),
    )

    assert result["required_monthly_total"] == 500
    assert result["available_monthly"] == 500
    assert result["shortfall"] == 0
    assert result["status"] == "feasible"
    assert [goal["allocated_monthly"] for goal in result["goals"]] == [300, 200]
    assert result["alternatives"] == []


def test_multiple_goals_reports_conflict_and_deadline_alternative():
    result = plan_multiple_goals(
        monthly_available=350,
        goals=[
            {
                "name": "First",
                "target_amount": 1_200,
                "current_amount": 0,
                "target_date": "2026-12-31",
                "priority": 1,
            },
            {
                "name": "Second",
                "target_amount": 800,
                "current_amount": 0,
                "target_date": "2026-12-31",
                "priority": 2,
            },
        ],
        as_of=date(2026, 8, 23),
    )

    assert result["shortfall"] == 150
    assert result["status"] == "infeasible"
    assert [goal["allocated_monthly"] for goal in result["goals"]] == [300, 50]
    assert result["alternatives"] == [
        {
            "type": "extend_deadline",
            "goal": "Second",
            "new_target_date": "2027-12-31",
        }
    ]


def test_multiple_goals_preserves_input_order_for_equal_priority_and_skips_funded():
    result = plan_multiple_goals(
        monthly_available=300,
        goals=[
            {
                "name": "Funded",
                "target_amount": 100,
                "current_amount": 100,
                "target_date": "2026-12-31",
                "priority": 1,
            },
            {
                "name": "First equal",
                "target_amount": 800,
                "current_amount": 0,
                "target_date": "2026-12-31",
                "priority": 2,
            },
            {
                "name": "Second equal",
                "target_amount": 800,
                "current_amount": 0,
                "target_date": "2026-12-31",
                "priority": 2,
            },
        ],
        as_of=date(2026, 8, 23),
    )

    assert [goal["name"] for goal in result["goals"]] == [
        "Funded",
        "First equal",
        "Second equal",
    ]
    assert [goal["allocated_monthly"] for goal in result["goals"]] == [0, 200, 100]


def test_financial_scenario_baseline_keeps_cash_debt_and_net_worth_separate(decision_db):
    result = run_financial_scenario(
        decision_db,
        horizon_months=12,
        scenario={},
        as_of=date(2026, 8, 23),
    )

    assert result["starting_position"]["liquid_funds"] == 300_000
    assert result["starting_position"]["debt"] == 1_000
    assert result["ending_position"]["liquid_funds"] == 900_000
    assert result["ending_position"]["debt"] == 1_000
    assert result["cash_flow"][0]["month_start_balance"] == 300_000
    assert result["cash_flow"][0]["income"] == 200_000
    assert result["cash_flow"][0]["baseline_expenses"] == 150_000
    assert result["cash_flow"][0]["month_end_balance"] == 350_000


@pytest.mark.parametrize(
    ("scenario", "ending_cash"),
    [
        ({"income_change_pct": -20}, 420_000),
        ({"expense_change_pct": 10}, 720_000),
        ({"one_time_expenses": [{"month": 4, "amount": 300_000}]}, 600_000),
    ],
)
def test_financial_scenario_applies_explicit_deterministic_changes(
    decision_db, scenario, ending_cash
):
    result = run_financial_scenario(
        decision_db,
        horizon_months=12,
        scenario=scenario,
        as_of=date(2026, 8, 23),
    )

    assert result["ending_position"]["liquid_funds"] == ending_cash


def test_financial_scenario_extra_debt_payment_is_capped_at_debt(decision_db):
    result = run_financial_scenario(
        decision_db,
        horizon_months=12,
        scenario={"monthly_extra_debt_payment": 300},
        as_of=date(2026, 8, 23),
    )

    assert [row["extra_debt_payment"] for row in result["cash_flow"][:4]] == [
        300,
        300,
        300,
        100,
    ]
    assert result["ending_position"]["debt"] == 0
    assert result["ending_position"]["liquid_funds"] == 899_000
    assert result["ending_position"]["net_worth"] == result["starting_position"]["net_worth"] + 600_000


def test_financial_scenario_tracks_liquidity_crossing_zero(decision_db):
    result = run_financial_scenario(
        decision_db,
        horizon_months=3,
        minimum_liquidity_buffer=100_000,
        scenario={"income_change_pct": -100},
        as_of=date(2026, 8, 23),
    )

    assert result["minimum_liquidity"] == {"amount": -150_000, "month": 3}
    assert result["months_below_buffer"] == [2, 3]
    assert "liquidity_below_zero" in result["warnings"]
    assert "minimum_liquidity_buffer_breached" in result["warnings"]


def test_financial_scenario_caps_goal_balance_at_target(decision_db):
    result = run_financial_scenario(
        decision_db,
        horizon_months=4,
        scenario={
            "goals": [
                {
                    "name": "Trip",
                    "current_amount": 0,
                    "target_amount": 250,
                    "monthly_contribution": 100,
                }
            ]
        },
        as_of=date(2026, 8, 23),
    )

    assert [row["goal_contributions"]["Trip"] for row in result["cash_flow"]] == [
        100,
        100,
        50,
        0,
    ]
    assert result["ending_position"]["goal_balances"] == {"Trip": 250}


@pytest.mark.parametrize("horizon", [12, 24, 60])
def test_financial_scenario_supports_planning_horizons(decision_db, horizon):
    result = run_financial_scenario(
        decision_db,
        horizon_months=horizon,
        scenario={},
        as_of=date(2026, 8, 23),
    )

    assert len(result["cash_flow"]) == horizon
    assert result["cash_flow"][-1]["month"] == horizon


@pytest.mark.parametrize("horizon", [0, 121])
def test_financial_scenario_validates_horizon(decision_db, horizon):
    with pytest.raises(InputValidationError):
        run_financial_scenario(decision_db, horizon_months=horizon, scenario={})


def test_financial_scenario_validates_one_time_month(decision_db):
    with pytest.raises(InputValidationError):
        run_financial_scenario(
            decision_db,
            horizon_months=12,
            scenario={"one_time_expenses": [{"month": 13, "amount": 1}]},
        )


def test_financial_scenario_money_invariants_hold(decision_db):
    result = run_financial_scenario(
        decision_db,
        horizon_months=12,
        scenario={
            "income_change_pct": -20,
            "expense_change_pct": 10,
            "one_time_expenses": [{"month": 4, "amount": 300_000}],
            "monthly_extra_debt_payment": 300,
        },
        as_of=date(2026, 8, 23),
    )

    previous_cash = result["starting_position"]["liquid_funds"]
    previous_debt = result["starting_position"]["debt"]
    for row in result["cash_flow"]:
        assert row["month_start_balance"] == previous_cash
        assert row["month_end_balance"] == pytest.approx(
            previous_cash
            + row["income"]
            - row["baseline_expenses"]
            - row["one_time_expenses"]
            - row["extra_debt_payment"]
            - sum(row["goal_contributions"].values())
        )
        assert row["ending_debt"] >= 0
        assert row["ending_debt"] == previous_debt - row["extra_debt_payment"]
        previous_cash = row["month_end_balance"]
        previous_debt = row["ending_debt"]


def test_financial_plan_deducts_minimum_debt_payment_then_prioritizes_reserve(decision_db):
    result = build_financial_plan(
        decision_db,
        planning_horizon_months=24,
        minimum_liquidity_buffer=100_000,
        emergency_fund={
            "target_months": 6,
            "essential_category_ids": ["essential"],
        },
        debt_accounts={"loan": {"apr_pct": 12, "minimum_payment": 100}},
        goals=[],
        as_of=date(2026, 8, 23),
    )

    allocation = result["monthly_allocation"]
    assert allocation == {
        "historical_net_cash_flow": 50_000,
        "minimum_debt_payments": 100,
        "free_cash_flow": 49_900,
        "liquidity_buffer": 0,
        "emergency_fund": 49_900,
        "extra_debt_payment": 0,
        "goals": [],
        "unallocated": 0,
    }
    assert result["priorities"][0]["type"] == "liquidity"
    assert result["priorities"][2]["type"] == "emergency_fund"
    assert result["recommended_action"]["type"] == "increase_emergency_fund"


def test_financial_plan_reduces_expensive_debt_after_reserve_is_funded(decision_db):
    result = build_financial_plan(
        decision_db,
        emergency_fund={"target_months": 1, "monthly_essential_override": 1_000},
        debt_accounts={"loan": {"apr_pct": 24, "minimum_payment": 100}},
        goals=[],
        as_of=date(2026, 8, 23),
    )

    assert result["monthly_allocation"]["minimum_debt_payments"] == 100
    assert result["monthly_allocation"]["extra_debt_payment"] == 920
    assert result["monthly_allocation"]["unallocated"] == 48_980
    assert result["recommended_action"]["type"] == "reduce_expensive_debt"


def test_financial_plan_postpones_goal_while_reserve_has_a_gap(decision_db):
    result = build_financial_plan(
        decision_db,
        emergency_fund={
            "target_months": 6,
            "essential_category_ids": ["essential"],
        },
        debt_accounts={"loan": {"apr_pct": 12, "minimum_payment": 100}},
        goals=[
            {
                "name": "Car",
                "target_amount": 200_000,
                "current_amount": 0,
                "target_date": "2026-12-31",
                "priority": 1,
            }
        ],
        as_of=date(2026, 8, 23),
    )

    assert result["monthly_allocation"]["goals"][0]["monthly_amount"] == 0
    assert result["monthly_allocation"]["goals"][0]["status"] == "underfunded"
    assert result["recommended_action"]["alternatives"][0]["type"] == "slower_reserve_build"


def test_financial_plan_healthy_state_leaves_cash_unallocated(decision_db):
    decision_db.connect().execute("UPDATE accounts SET balance=0 WHERE id='loan'")

    result = build_financial_plan(
        decision_db,
        emergency_fund={"target_months": 1, "monthly_essential_override": 1_000},
        debt_accounts={},
        goals=[],
        as_of=date(2026, 8, 23),
    )

    assert result["monthly_allocation"]["minimum_debt_payments"] == 0
    assert result["monthly_allocation"]["extra_debt_payment"] == 0
    assert result["monthly_allocation"]["goals"] == []
    assert result["monthly_allocation"]["unallocated"] == 50_000
    assert result["recommended_action"]["type"] == "allocate_remaining_free_cash_flow"
    assert result["warnings"] == []


def test_financial_plan_reports_negative_free_cash_flow(decision_db):
    decision_db.connect().execute(
        "UPDATE transactions SET income=100000 WHERE id LIKE 'income-%'"
    )

    result = build_financial_plan(
        decision_db,
        emergency_fund={"target_months": 1, "monthly_essential_override": 1_000},
        debt_accounts={"loan": {"apr_pct": 12, "minimum_payment": 100}},
        goals=[],
        as_of=date(2026, 8, 23),
    )

    assert result["monthly_allocation"]["historical_net_cash_flow"] == -50_000
    assert result["monthly_allocation"]["free_cash_flow"] == 0
    assert "negative_free_cash_flow" in result["warnings"]
    assert result["recommended_action"]["type"] == "restore_positive_cash_flow"


def test_financial_plan_returns_all_configuration_gaps(decision_db):
    result = build_financial_plan(
        decision_db,
        emergency_fund={"target_months": 6, "essential_category_ids": []},
        debt_accounts={},
        goals=[],
        as_of=date(2026, 8, 23),
    )

    assert result["status"] == "configuration_required"
    assert {item["field"] for item in result["missing"]} == {
        "essential_category_ids or monthly_essential_override",
        "debt_accounts.loan",
    }


def test_financial_plan_validates_nested_configuration_shapes(decision_db):
    with pytest.raises(InputValidationError):
        build_financial_plan(
            decision_db,
            emergency_fund=[],
            debt_accounts={},
            goals=[],
        )
    with pytest.raises(InputValidationError):
        build_financial_plan(
            decision_db,
            emergency_fund={"monthly_essential_override": 1_000},
            debt_accounts={},
            goals={},
        )


def test_financial_plan_exposes_policy_and_never_double_allocates_cash(decision_db):
    result = build_financial_plan(
        decision_db,
        emergency_fund={"target_months": 1, "monthly_essential_override": 1_000},
        debt_accounts={"loan": {"apr_pct": 24, "minimum_payment": 100}},
        goals=[
            {
                "name": "Course",
                "target_amount": 1_000,
                "current_amount": 0,
                "target_date": "2026-12-31",
                "priority": 1,
            }
        ],
        as_of=date(2026, 8, 23),
    )

    assert [item["type"] for item in result["priority_policy"]] == [
        "preserve_minimum_liquidity_buffer",
        "cover_essential_upcoming_obligations",
        "reach_minimum_emergency_fund",
        "meet_minimum_debt_payments",
        "reduce_expensive_debt",
        "fund_high_priority_goals",
        "allocate_remaining_free_cash_flow",
    ]
    allocation = result["monthly_allocation"]
    allocated = (
        allocation["minimum_debt_payments"]
        + allocation["liquidity_buffer"]
        + allocation["emergency_fund"]
        + allocation["extra_debt_payment"]
        + sum(goal["monthly_amount"] for goal in allocation["goals"])
        + allocation["unallocated"]
    )
    assert allocated == allocation["historical_net_cash_flow"]
