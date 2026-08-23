import json

import pytest

from tests.test_decision import decision_db
from zenmoney_mcp import server


@pytest.fixture
def decision_mcp_db(decision_db):
    previous = server._db
    server._db = decision_db
    try:
        yield decision_db
    finally:
        server._db = previous


@pytest.mark.asyncio
async def test_decision_tool_discovery_has_strict_bounded_schemas():
    tools = {tool.name: tool.inputSchema for tool in await server.list_tools()}

    names = {
        "plan_emergency_fund",
        "plan_debt_payoff",
        "compare_debt_strategies",
        "plan_financial_goal",
        "plan_multiple_goals",
        "run_financial_scenario",
        "build_financial_plan",
    }
    assert names <= tools.keys()
    assert tools["plan_emergency_fund"]["properties"]["allocation_pct_of_free_cash_flow"] == {
        "type": "number",
        "minimum": 0,
        "maximum": 100,
        "default": 75,
        "description": "Percentage of non-negative trailing free cash flow allocated monthly",
    }
    debt = tools["plan_debt_payoff"]
    assert debt["properties"]["strategy"]["enum"] == [
        "minimum_only",
        "avalanche",
        "snowball",
        "custom",
    ]
    assert debt["properties"]["debt_accounts"]["maxProperties"] == 50
    goal = tools["plan_financial_goal"]
    assert goal["properties"]["target_date"]["pattern"] == r"^\d{4}-\d{2}-\d{2}$"
    assert goal["properties"]["annual_return_pct"]["const"] == 0
    assert len(goal["oneOf"]) == 2
    multiple = tools["plan_multiple_goals"]
    assert multiple["properties"]["goals"]["maxItems"] == 50
    scenario = tools["run_financial_scenario"]
    assert scenario["properties"]["horizon_months"]["minimum"] == 1
    assert scenario["properties"]["horizon_months"]["maximum"] == 120
    assert scenario["properties"]["scenario_name"]["enum"] == [
        "negative",
        "base",
        "positive",
    ]
    plan = tools["build_financial_plan"]
    assert plan["properties"]["planning_horizon_months"]["maximum"] == 120
    assert plan["properties"]["goals"]["maxItems"] == 50
    assert all(schema.get("additionalProperties") is False for schema in tools.values() if schema in [tools[name] for name in names])
    assert not ({"execute_plan", "apply_plan", "create_transfer", "modify_budget"} & tools.keys())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments", "expected_key"),
    [
        (
            "plan_emergency_fund",
            {"monthly_essential_override": 120_000, "target_months": 6},
            "plan",
        ),
        (
            "plan_debt_payoff",
            {
                "strategy": "avalanche",
                "debt_accounts": {"loan": {"apr_pct": 12, "minimum_payment": 100}},
            },
            "schedule",
        ),
        (
            "compare_debt_strategies",
            {
                "monthly_extra_payment": 100,
                "debt_accounts": {"loan": {"apr_pct": 12, "minimum_payment": 100}},
            },
            "strategies",
        ),
        (
            "plan_financial_goal",
            {"name": "Course", "target_amount": 1_200, "target_date": "2026-12-31"},
            "goal",
        ),
        (
            "plan_multiple_goals",
            {"monthly_available": 500, "goals": []},
            "goals",
        ),
        (
            "run_financial_scenario",
            {"horizon_months": 12, "scenario": {}},
            "ending_position",
        ),
        (
            "build_financial_plan",
            {
                "emergency_fund": {"target_months": 1, "monthly_essential_override": 1_000},
                "debt_accounts": {"loan": {"apr_pct": 12, "minimum_payment": 100}},
                "goals": [],
            },
            "recommended_action",
        ),
    ],
)
async def test_decision_tool_dispatch_returns_structured_json(
    decision_mcp_db, name, arguments, expected_key
):
    content = await server.call_tool(name, arguments)

    result = json.loads(content[0].text)
    assert expected_key in result
    assert isinstance(result, dict)
    assert result["data_quality"] in {"high", "medium", "low"}
    assert isinstance(result["limitations"], list)


@pytest.mark.asyncio
async def test_decision_tools_are_cache_only(decision_mcp_db, monkeypatch):
    monkeypatch.setattr(
        server,
        "get_sync_engine",
        lambda: (_ for _ in ()).throw(AssertionError("planning attempted network sync")),
    )

    content = await server.call_tool(
        "run_financial_scenario",
        {"horizon_months": 1, "scenario": {}},
    )

    assert json.loads(content[0].text)["horizon_months"] == 1
