import json

import pytest
from jsonschema import validate

from zenmoney_mcp import server
from zenmoney_mcp.hardened_database import HardenedDatabase


@pytest.fixture
def planning_mcp_db():
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
    conn.execute(
        "INSERT INTO accounts(id,title,type,instrument,balance,in_balance,savings,archive,user,changed) "
        "VALUES ('cash','Cash','checking',1,1000,1,0,0,1,1)"
    )
    conn.execute(
        "INSERT INTO transactions(id,date,user,deleted,hold,income,income_instrument,income_account,"
        "outcome,outcome_instrument,outcome_account,changed) "
        "VALUES ('income','2026-07-01',1,0,0,500,1,'cash',0,1,'cash',1)"
    )
    conn.commit()
    previous = server._db
    server._db = db
    try:
        yield db
    finally:
        server._db = previous
        db.close()


@pytest.mark.asyncio
async def test_planning_tool_discovery_has_strict_schemas():
    tools = {tool.name: tool.input_schema for tool in await server.list_tools()}

    assert {
        "get_financial_snapshot",
        "get_cash_flow",
        "get_spending_baseline",
        "compare_periods",
        "get_emergency_fund_status",
        "get_debt_service",
        "forecast_cash_flow",
    } <= tools.keys()
    cash = tools["get_cash_flow"]["properties"]
    assert cash["period"]["enum"] == [
        "current_period",
        "last_complete_month",
        "last_30_days",
        "trailing_3_complete_months",
        "trailing_6_complete_months",
        "trailing_12_complete_months",
    ]
    assert cash["start_date"]["pattern"] == r"^\d{4}-\d{2}-\d{2}$"
    baseline = tools["get_spending_baseline"]["properties"]["months"]
    assert (baseline["minimum"], baseline["maximum"]) == (3, 24)
    comparison = tools["compare_periods"]["properties"]
    assert comparison["period_a"]["required"] == ["start_date", "end_date"]
    assert comparison["preset"]["enum"] == [
        "last_month_vs_previous",
        "last_quarter_vs_previous",
        "last_complete_month_vs_year_ago",
    ]
    emergency = tools["get_emergency_fund_status"]["properties"]
    assert emergency["essential_category_ids"]["uniqueItems"] is True
    assert emergency["monthly_essential_override"]["minimum"] == 0
    assert tools["forecast_cash_flow"]["properties"]["horizon_days"]["enum"] == [
        30,
        60,
        90,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "period",
    [
        "current_period",
        "last_complete_month",
        "last_30_days",
        "trailing_3_complete_months",
        "trailing_6_complete_months",
        "trailing_12_complete_months",
    ],
)
async def test_cash_flow_schema_accepts_every_documented_period(period):
    tools = {tool.name: tool.input_schema for tool in await server.list_tools()}
    schema = tools["get_cash_flow"]

    validate({"period": period}, schema)
    assert "pattern" not in schema["properties"]["period"]


@pytest.mark.asyncio
async def test_planning_tool_dispatch_returns_json(planning_mcp_db):
    content = await server.call_tool(
        "get_cash_flow",
        {"start_date": "2026-07-01", "end_date": "2026-07-31"},
    )

    result = json.loads(content[0].text)
    assert result["income"] == 500
    assert result["currency"] == "RUB"


@pytest.mark.asyncio
async def test_financial_snapshot_resource_is_cache_only(planning_mcp_db, monkeypatch):
    monkeypatch.setattr(
        server,
        "get_sync_engine",
        lambda: (_ for _ in ()).throw(AssertionError("resource attempted sync")),
    )

    resources = await server.list_resources()
    payload = json.loads(await server.read_resource("zenmoney://financial-snapshot"))

    assert "zenmoney://financial-snapshot" in {str(resource.uri) for resource in resources}
    assert payload["net_worth"] == 1000
