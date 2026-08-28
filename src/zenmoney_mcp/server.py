"""MCP Server for ZenMoney financial analytics."""

import json
import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from mcp.server import Server
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CallToolResult,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    ListResourceTemplatesResult,
    ListResourcesResult,
    ListToolsResult,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
    TextContent,
    TextResourceContents,
    Tool,
    ToolAnnotations,
)

from . import __version__
from . import analytics as legacy_analytics
from .analytics import (
    get_categories_resource,
    get_current_budgets_resource,
    get_instruments_resource,
    get_sync_status_resource,
    suggest_category,
)
from .decision import (
    build_financial_plan,
    compare_debt_strategies,
    plan_debt_payoff,
    plan_emergency_fund,
    plan_financial_goal,
    plan_multiple_goals,
    run_financial_scenario,
)
from .entity_changes import EDITABLE, SAFE_DELETE
from .planning import (
    compare_periods,
    forecast_cash_flow,
    get_cash_flow,
    get_debt_service,
    get_emergency_fund_status,
    get_financial_position,
    get_financial_snapshot,
    get_spending_baseline,
)
from .financial_correctness import (
    analyze_income,
    analyze_merchants,
    analyze_spending,
    analyze_transfers,
    analyze_trends,
    check_budget_health,
    convert_currency,
    detect_anomalies,
    detect_recurring,
    get_account_flow,
    get_debts,
    get_exchange_rates,
    get_liquidity,
    get_net_worth,
    get_upcoming_payments,
    search_transactions,
)
from .financial_correctness import configure_legacy_analytics
from .hardened_database import HardenedDatabase
from .hardened_sync import HardenedSyncEngine
from .entity_resources import (
    EntityResourceError,
    get_entity_resource,
    list_entity_resource,
)
from .sync_control import (
    DEFAULT_CONTROL_PATH,
    InvalidSyncState,
    read_sync_state,
    request_sync,
)
from .mutations import (
    MutationStateError,
    MutationValidationError,
    ProposalStore,
    execute_proposal,
    get_change_proposal,
    prepare_changes,
    prepare_recurring_payment,
)


# Global state
_db: HardenedDatabase | None = None
_sync_engine: HardenedSyncEngine | None = None

configure_legacy_analytics(legacy_analytics)

REMOTE_EXCLUDED_TOOLS = frozenset({"sync_data", "suggest_category"})
REMOTE_CONTROL_TOOLS = frozenset({"force_sync", "get_sync_status"})
PREPARE_TOOL_ENTITIES = {
    "prepare_account_changes": "account",
    "prepare_tag_changes": "tag",
    "prepare_merchant_changes": "merchant",
    "prepare_reminder_changes": "reminder",
    "prepare_reminder_marker_changes": "reminderMarker",
    "prepare_transaction_changes": "transaction",
    "prepare_budget_changes": "budget",
}
MUTATION_TOOLS = frozenset(
    {
        *PREPARE_TOOL_ENTITIES,
        "prepare_changes",
        "prepare_mixed_changes",
        "prepare_recurring_payment",
        "get_change_proposal",
        "apply_changes",
    }
)
LOGGER = logging.getLogger(__name__)
ENTITY_RESOURCE_NAMES = {
    "accounts": "account",
    "tags": "tag",
    "merchants": "merchant",
    "reminders": "reminder",
    "reminder-markers": "reminderMarker",
    "transactions": "transaction",
    "budgets": "budget",
}
ENTITY_READ_TOOL_NAMES = {
    "account": ("list_accounts", "get_account"),
    "tag": ("list_tags", "get_tag"),
    "merchant": ("list_merchants", "get_merchant"),
    "reminder": ("list_reminders", "get_reminder"),
    "reminderMarker": ("list_reminder_markers", "get_reminder_marker"),
    "transaction": ("list_transactions", "get_transaction"),
    "budget": ("list_budgets", "get_budget"),
}
LIST_ENTITY_TOOLS = {
    names[0]: entity_type
    for entity_type, names in ENTITY_READ_TOOL_NAMES.items()
}
GET_ENTITY_TOOLS = {
    names[1]: entity_type
    for entity_type, names in ENTITY_READ_TOOL_NAMES.items()
}
ENTITY_READ_TOOLS = frozenset({*LIST_ENTITY_TOOLS, *GET_ENTITY_TOOLS})


def get_database_path() -> Path:
    """Return the configured local ZenMoney database path."""
    configured_path = os.environ.get("ZENMONEY_DB_PATH")
    if configured_path:
        return Path(configured_path)
    return Path.home() / ".cache" / "zenmoney-mcp" / "zenmoney.db"


def get_mutation_path() -> Path:
    """Return the local persistent transaction proposal path."""
    return get_database_path().with_name("mutation-proposals.db")


def get_db() -> HardenedDatabase:
    """Get or create database instance."""
    global _db
    if _db is None:
        db_path = get_database_path()
        cache_dir = db_path.parent
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_dir.chmod(0o700)

        _db = HardenedDatabase(db_path)
        _db.init_schema()
    return _db


def open_remote_db() -> HardenedDatabase:
    """Open the configured ZenMoney snapshot for remote read-only access."""
    db_path = get_database_path()
    if not db_path.is_file():
        raise FileNotFoundError(f"ZenMoney database does not exist: {db_path}")
    return HardenedDatabase(db_path, read_only=True)


def get_sync_engine() -> HardenedSyncEngine:
    """Get or create sync engine instance."""
    global _sync_engine
    if _sync_engine is None:
        token = os.environ.get("ZENMONEY_TOKEN")
        if not token:
            raise ValueError(
                "ZENMONEY_TOKEN environment variable is required. "
                "Get your token at https://zerro.app/token"
            )
        _sync_engine = HardenedSyncEngine(get_db(), token)
    return _sync_engine


def init_for_testing(db: HardenedDatabase, token: str = "test_token") -> None:
    """Initialize server with test database and token.

    Args:
        db: Database instance to use.
        token: OAuth token (can be dummy for testing without API).
    """
    global _db, _sync_engine
    _db = db
    _sync_engine = HardenedSyncEngine(db, token)


# ============================================================================
# Tools
# ============================================================================

_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
_MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
_PERIOD_PATTERN = r"^(this_month|last_month|last_30_days|\d{4}-(0[1-9]|1[0-2]))$"
_CURRENCY_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{1,11}$"
_PERIOD_SCHEMA = {
    "type": "object",
    "properties": {
        "start_date": {
            "type": "string",
            "pattern": _DATE_PATTERN,
            "description": "Inclusive period start in YYYY-MM-DD format",
        },
        "end_date": {
            "type": "string",
            "pattern": _DATE_PATTERN,
            "description": "Inclusive period end in YYYY-MM-DD format",
        },
    },
    "required": ["start_date", "end_date"],
    "additionalProperties": False,
}


def harden_tool_schemas(tools: list[Tool]) -> list[Tool]:
    """Apply the validation contract while descriptors are constructed."""
    integer_bounds = {
        "top_n": (1, 100),
        "limit": (1, 200),
        "months": (1, 60),
        "lookback_months": (1, 60),
        "days_ahead": (1, 366),
        "tolerance_pct": (0, 100),
    }
    non_negative = {"amount", "target_amount", "min_amount", "max_amount"}

    for tool in tools:
        properties = tool.input_schema.get("properties")
        if not isinstance(properties, dict):
            continue
        if tool.name == "analyze_spending":
            properties.pop("include_transfers", None)
        for name, value in properties.items():
            if not isinstance(value, dict):
                continue
            if name in integer_bounds:
                minimum, maximum = integer_bounds[name]
                value.setdefault("minimum", minimum)
                value.setdefault("maximum", maximum)
            if name in non_negative:
                value["minimum"] = 0
            if name in {"start_date", "end_date"}:
                value["pattern"] = _DATE_PATTERN
            elif name == "month":
                value["pattern"] = _MONTH_PATTERN
            elif name == "period" and "enum" not in value:
                value["pattern"] = _PERIOD_PATTERN
            elif name == "z_threshold":
                value["minimum"] = 1.5
                value["maximum"] = 10
            elif name in {"from_currency", "to_currency"}:
                value["pattern"] = _CURRENCY_PATTERN
                value["maxLength"] = 12
            elif name == "currencies":
                value["minItems"] = 1
                value["maxItems"] = 20
                value["uniqueItems"] = True
                items = value.setdefault("items", {"type": "string"})
                if isinstance(items, dict):
                    items["pattern"] = _CURRENCY_PATTERN
                    items["maxLength"] = 12
    return tools


def _planning_tools() -> list[Tool]:
    return [
        Tool(
            name="get_financial_snapshot",
            description="Get a compact read-only snapshot of current financial position and recent cash flow.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_cash_flow",
            description="Classify household income, operating spending, financing, transfers, and cash debt service; holds are excluded.",
            inputSchema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": [
                            "current_period",
                            "last_complete_month",
                            "last_30_days",
                            "trailing_3_complete_months",
                            "trailing_6_complete_months",
                            "trailing_12_complete_months",
                        ],
                        "default": "current_period",
                        "description": "Period preset; start_date and end_date select a custom range",
                    },
                    "start_date": {
                        "type": "string",
                        "pattern": _DATE_PATTERN,
                        "description": "Custom inclusive start date; requires end_date",
                    },
                    "end_date": {
                        "type": "string",
                        "pattern": _DATE_PATTERN,
                        "description": "Custom inclusive end date; requires start_date",
                    },
                },
            },
        ),
        Tool(
            name="get_spending_baseline",
            description="Get completed-month spending statistics with median as the normal-spending baseline.",
            inputSchema={
                "type": "object",
                "properties": {
                    "months": {
                        "type": "integer",
                        "minimum": 3,
                        "maximum": 24,
                        "default": 6,
                        "description": "Number of completed budget months",
                    },
                    "category_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Optional category ID; descendants are included",
                    },
                    "include_current_partial_month": {
                        "type": "boolean",
                        "default": False,
                    },
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="compare_periods",
            description="Compare income, spending, net cash flow, and category spending between two periods.",
            inputSchema={
                "type": "object",
                "properties": {
                    "preset": {
                        "type": "string",
                        "enum": [
                            "last_month_vs_previous",
                            "last_quarter_vs_previous",
                            "last_complete_month_vs_year_ago",
                        ],
                        "default": "last_month_vs_previous",
                        "description": "Comparison preset used unless period_a and period_b are supplied",
                    },
                    "period_a": {**_PERIOD_SCHEMA, "description": "Earlier custom period"},
                    "period_b": {**_PERIOD_SCHEMA, "description": "Later custom period"},
                },
            },
        ),
        Tool(
            name="get_emergency_fund_status",
            description="Measure reserve coverage using explicit essential categories or a monthly override.",
            inputSchema={
                "type": "object",
                "properties": {
                    "essential_category_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "maxItems": 100,
                        "uniqueItems": True,
                        "description": "Explicit essential category IDs; descendants are included",
                    },
                    "monthly_essential_override": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Explicit monthly essential-spending amount",
                    },
                    "baseline_months": {
                        "type": "integer",
                        "minimum": 3,
                        "maximum": 24,
                        "default": 6,
                        "description": "Completed months used for category baseline",
                    },
                    "target_months": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 60,
                        "default": 6,
                        "description": "Target reserve coverage in months",
                    },
                },
            },
        ),
        Tool(
            name="get_debt_service",
            description="Get every active financial obligation, cash debt service, and debt-service ratio without invented terms.",
            inputSchema={
                "type": "object",
                "properties": {
                    "obligation_overrides": _OBLIGATION_OVERRIDES_SCHEMA,
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="get_financial_position",
            description="Get one economic view of all active assets, liabilities, net worth, and monthly cash flow after debt service.",
            inputSchema={
                "type": "object",
                "properties": {
                    "obligation_overrides": _OBLIGATION_OVERRIDES_SCHEMA,
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="forecast_cash_flow",
            description="Get transparent 30/60/90-day scheduled, recurring, and baseline cash-flow scenarios.",
            inputSchema={
                "type": "object",
                "properties": {
                    "horizon_days": {
                        "type": "integer",
                        "enum": [30, 60, 90],
                        "default": 90,
                        "description": "Scenario horizon in days",
                    }
                },
            },
        ),
    ]


_DEBT_ACCOUNTS_SCHEMA = {
    "type": "object",
    "maxProperties": 50,
    "additionalProperties": {
        "type": "object",
        "properties": {
            "liability_type": {
                "type": "string",
                "enum": [
                    "fixed_loan",
                    "credit_card",
                    "installment",
                    "arbitrary",
                ],
                "description": "Explicit payoff model; inferred from the obligation class when omitted",
            },
            "title": {
                "type": "string",
                "minLength": 1,
                "description": "Display title, primarily for user-only arbitrary liabilities",
            },
            "balance": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": "Explicit scenario balance; required for a liability absent from ZenMoney",
            },
            "apr_pct": {
                "type": "number",
                "minimum": 0,
                "description": "Nominal annual percentage rate; defaults to zero only for installment and arbitrary liabilities",
            },
            "fixed_payment": {
                "type": "number",
                "minimum": 0,
                "description": "Fixed loan payment; mutually exclusive with minimum_payment",
            },
            "minimum_payment": {
                "type": "number",
                "minimum": 0,
                "description": "Monthly minimum for credit cards and arbitrary liabilities; legacy fixed-loan payment",
            },
            "statement_balance": {
                "type": "number",
                "minimum": 0,
                "description": "Credit-card statement balance, tracked separately from total debt",
            },
            "grace_period_payment": {
                "type": "number",
                "minimum": 0,
                "description": "Credit-card grace payment due on grace_period_due_date",
            },
            "grace_period_due_date": {
                "type": "string",
                "pattern": _DATE_PATTERN,
                "description": "Future credit-card grace deadline",
            },
            "payment_schedule": {
                "type": "array",
                "minItems": 1,
                "maxItems": 120,
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "pattern": _DATE_PATTERN},
                        "amount": {"type": "number", "exclusiveMinimum": 0},
                    },
                    "required": ["date", "amount"],
                    "additionalProperties": False,
                },
                "description": "Future dated payments for an installment",
            },
        },
        "additionalProperties": False,
    },
    "description": "Payoff configuration keyed by obligation ID; arbitrary user-only IDs are supported with explicit balance",
}

_OBLIGATION_OVERRIDES_SCHEMA = {
    "type": "object",
    "maxProperties": 50,
    "additionalProperties": {
        "type": "object",
        "minProperties": 1,
        "properties": {
            "classification": {
                "type": "string",
                "enum": [
                    "loan",
                    "credit_card",
                    "installment",
                    "personal_debt",
                    "other",
                ],
            },
            "minimum_payment": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "minimum": 0},
                    "due_date": {"type": "string", "pattern": _DATE_PATTERN},
                },
                "required": ["amount"],
                "additionalProperties": False,
            },
            "apr_pct": {"type": "number", "minimum": 0},
        },
        "additionalProperties": False,
    },
    "description": "Optional explicit terms keyed by active obligation account ID",
}

_GOAL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "description": "Goal name"},
        "target_amount": {"type": "number", "minimum": 0, "description": "Target amount"},
        "current_amount": {
            "type": "number",
            "minimum": 0,
            "default": 0,
            "description": "Amount already assigned to the goal",
        },
        "target_date": {
            "type": "string",
            "pattern": _DATE_PATTERN,
            "description": "Deadline in YYYY-MM-DD format",
        },
        "priority": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "Ascending numeric priority; 1 is highest",
        },
    },
    "required": ["name", "target_amount", "target_date", "priority"],
    "additionalProperties": False,
}


def _decision_tools() -> list[Tool]:
    return [
        Tool(
            name="plan_emergency_fund",
            description="Plan deterministic month-end contributions to an explicitly configured emergency-fund target.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_months": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 60,
                        "default": 6,
                        "description": "Target months of essential spending",
                    },
                    "essential_category_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "maxItems": 100,
                        "uniqueItems": True,
                        "description": "Explicit essential categories; descendants are included",
                    },
                    "monthly_essential_override": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Explicit monthly essential-spending amount",
                    },
                    "minimum_liquidity_buffer": {
                        "type": "number",
                        "minimum": 0,
                        "default": 0,
                        "description": "Minimum own liquid funds to preserve",
                    },
                    "allocation_pct_of_free_cash_flow": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 100,
                        "default": 75,
                        "description": "Percentage of non-negative trailing free cash flow allocated monthly",
                    },
                    "include_restricted_deposits": {
                        "type": "boolean",
                        "default": False,
                        "description": "Explicitly allow restricted deposits in eligible reserve",
                    },
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="plan_debt_payoff",
            description="Build a Decimal amortization schedule using minimum-only, avalanche, snowball, or explicit custom priority.",
            inputSchema={
                "type": "object",
                "properties": {
                    "monthly_extra_payment": {
                        "type": "number",
                        "minimum": 0,
                        "default": 0,
                        "description": "Monthly amount above configured minimum payments",
                    },
                    "strategy": {
                        "type": "string",
                        "enum": ["minimum_only", "avalanche", "snowball", "custom"],
                        "default": "avalanche",
                        "description": "Debt payment priority strategy",
                    },
                    "debt_accounts": _DEBT_ACCOUNTS_SCHEMA,
                    "custom_order": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "maxItems": 50,
                        "uniqueItems": True,
                        "description": "Every active debt account ID in custom priority order",
                    },
                },
                "required": ["debt_accounts"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="compare_debt_strategies",
            description="Compare minimum-only, snowball, and avalanche by payoff duration and total interest.",
            inputSchema={
                "type": "object",
                "properties": {
                    "monthly_extra_payment": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Monthly extra used by snowball and avalanche",
                    },
                    "debt_accounts": _DEBT_ACCOUNTS_SCHEMA,
                },
                "required": ["monthly_extra_payment", "debt_accounts"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="plan_financial_goal",
            description="Solve one zero-return goal by deadline or by explicit monthly contribution.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "description": "Goal name"},
                    "target_amount": {"type": "number", "minimum": 0, "description": "Target amount"},
                    "current_amount": {"type": "number", "minimum": 0, "default": 0, "description": "Amount already funded"},
                    "target_date": {"type": "string", "pattern": _DATE_PATTERN, "description": "Deadline mode date"},
                    "monthly_contribution": {"type": "number", "minimum": 0, "description": "Contribution mode amount"},
                    "priority": {"type": "string", "enum": ["low", "medium", "high"], "default": "medium", "description": "Reported goal priority"},
                    "annual_return_pct": {"type": "number", "const": 0, "default": 0, "description": "Phase 3 supports zero investment return only"},
                },
                "required": ["name", "target_amount"],
                "oneOf": [
                    {"required": ["target_date"], "not": {"required": ["monthly_contribution"]}},
                    {"required": ["monthly_contribution"], "not": {"required": ["target_date"]}},
                ],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="plan_multiple_goals",
            description="Detect goal conflicts with stable greedy allocation by explicit numeric priority.",
            inputSchema={
                "type": "object",
                "properties": {
                    "monthly_available": {"type": "number", "minimum": 0, "description": "Monthly amount available across goals"},
                    "goals": {"type": "array", "items": _GOAL_SCHEMA, "maxItems": 50, "description": "Configured goals"},
                },
                "required": ["monthly_available", "goals"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="run_financial_scenario",
            description="Run a deterministic month-end cash, debt, goal, and net-worth scenario without Monte Carlo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "horizon_months": {"type": "integer", "minimum": 1, "maximum": 120, "description": "Calendar-month horizon"},
                    "scenario_name": {"type": "string", "enum": ["negative", "base", "positive"], "default": "base", "description": "Categorical label; changes remain explicit"},
                    "minimum_liquidity_buffer": {"type": "number", "minimum": 0, "default": 0, "description": "Warning threshold"},
                    "scenario": {
                        "type": "object",
                        "properties": {
                            "income_change_pct": {"type": "number", "minimum": -100, "default": 0, "description": "Constant monthly income change"},
                            "expense_change_pct": {"type": "number", "minimum": -100, "default": 0, "description": "Constant monthly expense change"},
                            "one_time_expenses": {
                                "type": "array",
                                "maxItems": 120,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "month": {"type": "integer", "minimum": 1, "maximum": 120},
                                        "amount": {"type": "number", "minimum": 0},
                                    },
                                    "required": ["month", "amount"],
                                    "additionalProperties": False,
                                },
                                "description": "Explicit one-time costs by scenario month",
                            },
                            "monthly_extra_debt_payment": {"type": "number", "minimum": 0, "default": 0, "description": "Principal-only scenario reduction"},
                            "goals": {
                                "type": "array",
                                "maxItems": 50,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string", "minLength": 1},
                                        "target_amount": {"type": "number", "minimum": 0},
                                        "current_amount": {"type": "number", "minimum": 0, "default": 0},
                                        "monthly_contribution": {"type": "number", "minimum": 0},
                                    },
                                    "required": ["name", "target_amount", "monthly_contribution"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "required": ["horizon_months", "scenario"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="build_financial_plan",
            description="Orchestrate Phase 2 facts and configured reserve, debt, and goals into one structured allocation plan.",
            inputSchema={
                "type": "object",
                "properties": {
                    "planning_horizon_months": {"type": "integer", "minimum": 1, "maximum": 120, "default": 24, "description": "Plan horizon"},
                    "minimum_liquidity_buffer": {"type": "number", "minimum": 0, "default": 100000, "description": "Minimum liquid funds"},
                    "emergency_fund": {
                        "type": "object",
                        "properties": {
                            "target_months": {"type": "integer", "minimum": 1, "maximum": 60, "default": 6},
                            "essential_category_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 100, "uniqueItems": True},
                            "monthly_essential_override": {"type": "number", "minimum": 0},
                            "include_restricted_deposits": {"type": "boolean", "default": False},
                        },
                        "additionalProperties": False,
                        "description": "Explicit emergency-fund planning inputs",
                    },
                    "debt_accounts": _DEBT_ACCOUNTS_SCHEMA,
                    "goals": {"type": "array", "items": _GOAL_SCHEMA, "maxItems": 50, "description": "Configured goals"},
                },
                "required": ["emergency_fund", "debt_accounts", "goals"],
                "additionalProperties": False,
            },
        ),
    ]


def _mutation_tools() -> list[Tool]:
    def nullable(schema: dict[str, Any]) -> dict[str, Any]:
        return {"anyOf": [schema, {"type": "null"}]}

    string = {"type": "string", "minLength": 1}
    number = {"type": "number"}
    non_negative = {"type": "number", "minimum": 0}
    integer = {"type": "integer"}
    date_schema = {"type": "string", "pattern": _DATE_PATTERN}
    ref = {
        "anyOf": [
            string,
            {
                "type": "object",
                "properties": {"ref": string},
                "required": ["ref"],
                "additionalProperties": False,
            },
        ]
    }
    fields = {
        "title": string,
        "type": {
            "type": "string",
            "enum": [
                "cash", "ccard", "checking", "loan", "deposit", "emoney",
                "debt",
            ],
        },
        "instrument": integer,
        "company": nullable(integer),
        "role": nullable(integer),
        "syncID": nullable({"type": "array", "items": {"type": "string"}}),
        "startBalance": number,
        "creditLimit": nullable(non_negative),
        "inBalance": {"type": "boolean"},
        "savings": {"type": "boolean"},
        "enableCorrection": {"type": "boolean"},
        "enableSMS": {"type": "boolean"},
        "capitalization": nullable({"type": "boolean"}),
        "percent": nullable(
            {"type": "number", "minimum": 0, "exclusiveMaximum": 100}
        ),
        "startDate": nullable(date_schema),
        "endDateOffset": nullable({"type": "integer", "minimum": 0}),
        "endDateOffsetInterval": nullable(
            {"type": "string", "enum": ["day", "week", "month", "year"]}
        ),
        "payoffStep": nullable({"type": "integer", "minimum": 0}),
        "payoffInterval": nullable(
            {"type": "string", "enum": ["month", "year"]}
        ),
        "parent": nullable(ref),
        "icon": nullable(string),
        "picture": nullable(string),
        "color": nullable(
            {"type": "integer", "minimum": 0, "maximum": 4294967295}
        ),
        "showIncome": {"type": "boolean"},
        "showOutcome": {"type": "boolean"},
        "budgetIncome": {"type": "boolean"},
        "budgetOutcome": {"type": "boolean"},
        "required": nullable({"type": "boolean"}),
        "incomeInstrument": integer,
        "incomeAccount": ref,
        "income": non_negative,
        "outcomeInstrument": integer,
        "outcomeAccount": ref,
        "outcome": non_negative,
        "tag": nullable({"type": "array", "items": ref, "uniqueItems": True}),
        "merchant": nullable(ref),
        "payee": nullable(string),
        "comment": nullable(string),
        "interval": nullable(
            {"type": "string", "enum": ["day", "week", "month", "year"]}
        ),
        "step": nullable({"type": "integer", "minimum": 1}),
        "points": nullable(
            {"type": "array", "items": {"type": "integer", "minimum": 0}}
        ),
        "endDate": nullable(date_schema),
        "notify": {"type": "boolean"},
        "date": date_schema,
        "reminder": ref,
        "state": {"type": "string", "enum": ["planned", "processed"]},
        "opIncome": nullable(non_negative),
        "opOutcome": nullable(non_negative),
        "opIncomeInstrument": nullable(integer),
        "opOutcomeInstrument": nullable(integer),
        "latitude": nullable(
            {"type": "number", "minimum": -90, "maximum": 90}
        ),
        "longitude": nullable(
            {"type": "number", "minimum": -180, "maximum": 180}
        ),
        "incomeLock": {"type": "boolean"},
        "outcomeLock": {"type": "boolean"},
    }
    create_required = {
        "account": {"title", "type", "instrument", "startBalance"},
        "tag": {"title"},
        "merchant": {"title"},
        "reminder": {
            "incomeInstrument", "incomeAccount", "income",
            "outcomeInstrument", "outcomeAccount", "outcome", "startDate",
        },
        "reminderMarker": {
            "incomeInstrument", "incomeAccount", "income",
            "outcomeInstrument", "outcomeAccount", "outcome", "date",
            "reminder", "state",
        },
        "transaction": {
            "incomeInstrument", "incomeAccount", "income",
            "outcomeInstrument", "outcomeAccount", "outcome", "date",
        },
        "budget": {
            "date", "tag", "income", "incomeLock", "outcome", "outcomeLock",
        },
    }

    def field_schema(entity_type: str, name: str) -> dict[str, Any]:
        if entity_type == "budget" and name == "tag":
            return deepcopy(nullable(ref))
        if entity_type == "reminder" and name == "startDate":
            return deepcopy(date_schema)
        return deepcopy(fields[name])

    def operation_schemas(
        entity_type: str, *, mixed: bool = False
    ) -> list[dict[str, Any]]:
        create_fields = set(EDITABLE[entity_type])
        if entity_type == "account":
            create_fields.add("startBalance")
        elif entity_type == "budget":
            create_fields.update({"date", "tag"})
        identity_name = "key" if entity_type == "budget" else "id"
        identity_schema = (
            {
                "type": "object",
                "properties": {
                    "owner_user_id": integer,
                    "tag": nullable(string),
                    "date": date_schema,
                },
                "required": ["owner_user_id", "tag", "date"],
                "additionalProperties": False,
            }
            if entity_type == "budget"
            else string
        )
        common = {"entity": {"const": entity_type}} if mixed else {}
        required_common = ["entity"] if mixed else []
        create_properties = {
            **common,
            "operation": {"const": "create"},
            "owner_user_id": integer,
            "value": {
                "type": "object",
                "properties": {
                    name: field_schema(entity_type, name)
                    for name in create_fields
                },
                "required": sorted(create_required[entity_type]),
                "additionalProperties": False,
            },
        }
        if entity_type != "budget":
            create_properties["ref"] = string
        operations = [
            {
                "type": "object",
                "properties": create_properties,
                "required": [*required_common, "operation", "value"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    **common,
                    "operation": {"const": "update"},
                    identity_name: identity_schema,
                    "set": {
                        "type": "object",
                        "minProperties": 1,
                        "properties": {
                            name: field_schema(entity_type, name)
                            for name in EDITABLE[entity_type]
                        },
                        "additionalProperties": False,
                    },
                },
                "required": [*required_common, "operation", identity_name, "set"],
                "additionalProperties": False,
            },
        ]
        if entity_type == "transaction":
            operations.append(
                {
                    "type": "object",
                    "properties": {
                        **common,
                        "operation": {"const": "split"},
                        "transaction_id": string,
                        "parts": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 100,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "amount": {
                                        "oneOf": [
                                            {
                                                "type": "number",
                                                "exclusiveMinimum": 0,
                                            },
                                            {"const": "remainder"},
                                        ]
                                    },
                                    "category_id": ref,
                                },
                                "required": ["amount", "category_id"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": [
                        *required_common,
                        "operation",
                        "transaction_id",
                        "parts",
                    ],
                    "additionalProperties": False,
                }
            )
        if entity_type in SAFE_DELETE:
            operations.append(
                {
                    "type": "object",
                    "properties": {
                        **common,
                        "operation": {"const": "delete"},
                        identity_name: identity_schema,
                    },
                    "required": [*required_common, "operation", identity_name],
                    "additionalProperties": False,
                }
            )
        return operations

    def prepare_schema(entity_type: str | None) -> dict[str, Any]:
        item_schemas = (
            operation_schemas(entity_type)
            if entity_type is not None
            else [
                schema
                for current_type in PREPARE_TOOL_ENTITIES.values()
                for schema in operation_schemas(current_type, mixed=True)
            ]
        )
        return {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {"oneOf": item_schemas},
                }
            },
            "required": ["operations"],
            "additionalProperties": False,
        }

    proposal_schema = {
        "type": "object",
        "properties": {
            "proposal_id": {"type": "string", "format": "uuid"},
        },
        "required": ["proposal_id"],
        "additionalProperties": False,
    }
    prepare_tools = [
        Tool(
            name=name,
            description=f"Prepare immutable {entity_type} changes for review without writing to ZenMoney.",
            inputSchema=prepare_schema(entity_type),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, openWorldHint=False
            ),
        )
        for name, entity_type in PREPARE_TOOL_ENTITIES.items()
    ]
    mixed_schema = prepare_schema(None)
    recurring_payment_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "amount": {"type": "number", "exclusiveMinimum": 0},
            "account_id": {"type": "string", "minLength": 1},
            "category_id": {"type": "string", "minLength": 1},
            "frequency": {"const": "monthly"},
            "day_of_month": {"type": "integer", "minimum": 1, "maximum": 31},
            "start_date": {"type": "string", "pattern": _DATE_PATTERN},
            "end_date": nullable(date_schema),
            "notify": {"type": "boolean"},
        },
        "required": [
            "name", "amount", "account_id", "category_id", "frequency",
            "day_of_month", "start_date", "end_date", "notify",
        ],
        "additionalProperties": False,
    }
    prepare_tools.extend(
        Tool(
            name=name,
            description="Prepare one immutable cross-entity change set for review without writing to ZenMoney.",
            inputSchema=mixed_schema,
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, openWorldHint=False
            ),
        )
        for name in ("prepare_changes", "prepare_mixed_changes")
    )
    return prepare_tools + [
        Tool(
            name="prepare_recurring_payment",
            description="Prepare one monthly recurring expense and its first planned marker for review without writing to ZenMoney.",
            inputSchema=recurring_payment_schema,
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, openWorldHint=False
            ),
        ),
        Tool(
            name="get_change_proposal",
            description="Read a prepared or executed change proposal by ID.",
            inputSchema=proposal_schema,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, openWorldHint=False
            ),
        ),
        Tool(
            name="apply_changes",
            description="Apply the exact previously reviewed change proposal.",
            inputSchema=proposal_schema,
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=True, openWorldHint=True
            ),
        ),
    ]


def _entity_read_tools() -> list[Tool]:
    """Expose existing entity resources as ChatGPT-visible read tools."""
    tools: list[Tool] = []
    for entity_type, (list_name, get_name) in ENTITY_READ_TOOL_NAMES.items():
        tools.append(
            Tool(
                name=list_name,
                description=f"List paginated ZenMoney {entity_type} entities.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "default": 50,
                            "description": "Page size from 1 to 200",
                        },
                        "cursor": {
                            "type": "string",
                            "description": "Opaque cursor from the previous page",
                        },
                        "include_inactive": {
                            "type": "boolean",
                            "default": False,
                            "description": "Include archived or deleted entities",
                        },
                    },
                    "additionalProperties": False,
                },
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    openWorldHint=False,
                ),
            )
        )
        identity = (
            {
                "type": "object",
                "properties": {
                    "owner_user_id": {"type": "integer"},
                    "tag": {
                        "anyOf": [{"type": "string"}, {"type": "null"}]
                    },
                    "date": {"type": "string", "pattern": _DATE_PATTERN},
                },
                "required": ["owner_user_id", "tag", "date"],
                "additionalProperties": False,
            }
            if entity_type == "budget"
            else {"type": "string", "minLength": 1}
        )
        identity_name = "key" if entity_type == "budget" else "id"
        tools.append(
            Tool(
                name=get_name,
                description=f"Get one exact ZenMoney {entity_type} entity.",
                inputSchema={
                    "type": "object",
                    "properties": {identity_name: identity},
                    "required": [identity_name],
                    "additionalProperties": False,
                },
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    openWorldHint=False,
                ),
            )
        )
    return tools

async def list_tools(remote: bool = False) -> list[Tool]:
    """List available tools."""
    tools = harden_tool_schemas([
        Tool(
            name="sync_data",
            description="Sync data with ZenMoney. Use to refresh data before analysis.",
            inputSchema={
                "type": "object",
                "properties": {
                    "force_full": {
                        "type": "boolean",
                        "description": "Force full sync (reset cache)",
                        "default": False,
                    }
                },
            },
        ),
        Tool(
            name="get_net_worth",
            description="Get total net worth: sum of all accounts broken down by type (current, savings, loans, debts).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_liquidity",
            description="Get liquid funds: how much cash is available. Answers: 'Can I afford this purchase?', 'How much cash do I have?'",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_amount": {
                        "type": "number",
                        "description": "Target purchase amount to check affordability",
                    },
                },
            },
        ),
        Tool(
            name="analyze_spending",
            description="Analyze spending by category. Answers: 'Where does my money go?', 'What do I spend the most on?'",
            inputSchema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Period: 'this_month', 'last_month', 'last_30_days' or 'YYYY-MM'",
                        "default": "this_month",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Custom start date (ISO, e.g. '2026-01-01'). Overrides period.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Custom end date (ISO). If omitted with start_date, defaults to today.",
                    },
                    "category_id": {
                        "type": "string",
                        "description": "Category UUID for drill-down (includes subcategories)",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Number of top categories to return",
                        "default": 10,
                    },
                    "include_transfers": {
                        "type": "boolean",
                        "description": "Include transfers between own accounts",
                        "default": False,
                    },
                    "include_holds": {
                        "type": "boolean",
                        "description": "Include hold transactions (pre-authorizations)",
                        "default": False,
                    },
                    "group_by": {
                        "type": "string",
                        "enum": ["category", "merchant"],
                        "description": "Aggregation mode: 'category' (default) or 'merchant'",
                        "default": "category",
                    },
                },
            },
        ),
        Tool(
            name="analyze_income",
            description="Analyze income by category and source. Answers: 'Where does my money come from?', 'How much did I earn?'",
            inputSchema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Period: 'this_month', 'last_month', 'last_30_days' or 'YYYY-MM'",
                        "default": "this_month",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Custom start date (ISO). Overrides period.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Custom end date (ISO). If omitted with start_date, defaults to today.",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Number of top categories/sources to return",
                        "default": 10,
                    },
                },
            },
        ),
        Tool(
            name="analyze_merchants",
            description="Analyze spending by merchant/store. Answers: 'Where do I spend the most?', 'Top stores'",
            inputSchema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Period: 'this_month', 'last_month', 'last_30_days' or 'YYYY-MM'",
                        "default": "this_month",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Custom start date (ISO). Overrides period.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Custom end date (ISO). If omitted with start_date, defaults to today.",
                    },
                    "category_id": {
                        "type": "string",
                        "description": "Category UUID to filter (includes subcategories)",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Number of top merchants to return",
                        "default": 10,
                    },
                },
            },
        ),
        Tool(
            name="check_budget_health",
            description="Check budget health: planned vs actual spending. Answers: 'Am I within budget?', 'Where am I overspending?'",
            inputSchema={
                "type": "object",
                "properties": {
                    "month": {
                        "type": "string",
                        "description": "Month in 'YYYY-MM' format. Defaults to current budget period.",
                    },
                },
            },
        ),
        Tool(
            name="get_upcoming_payments",
            description="Get upcoming payments from reminders. Answers: 'What payments are coming up?', 'What bills are due?'",
            inputSchema={
                "type": "object",
                "properties": {
                    "days_ahead": {
                        "type": "integer",
                        "description": "Planning horizon in days",
                        "default": 30,
                    },
                },
            },
        ),
        Tool(
            name="analyze_trends",
            description="Analyze spending/income trends over multiple months. Answers: 'How did my spending change?', 'Am I spending more?'",
            inputSchema={
                "type": "object",
                "properties": {
                    "months": {
                        "type": "integer",
                        "description": "Number of months to analyze",
                        "default": 6,
                    },
                    "category_id": {
                        "type": "string",
                        "description": "Category UUID to filter",
                    },
                    "metric": {
                        "type": "string",
                        "enum": ["outcome", "income", "savings_rate", "net_cashflow"],
                        "description": "Metric: outcome (spending), income, savings_rate (% saved), net_cashflow",
                        "default": "outcome",
                    },
                },
            },
        ),
        Tool(
            name="detect_recurring",
            description="Detect recurring payments (subscriptions, bills). Answers: 'What subscriptions do I have?', 'What can I cancel?'",
            inputSchema={
                "type": "object",
                "properties": {
                    "lookback_months": {
                        "type": "integer",
                        "description": "Analysis depth in months",
                        "default": 3,
                    },
                    "tolerance_pct": {
                        "type": "integer",
                        "description": "Amount variation tolerance in %",
                        "default": 10,
                    },
                },
            },
        ),
        Tool(
            name="get_account_flow",
            description="Get money flow for a specific account. Answers: 'What happened on my card?', 'Cash flow details'",
            inputSchema={
                "type": "object",
                "properties": {
                    "account_id": {
                        "type": "string",
                        "description": "Account UUID",
                    },
                    "period": {
                        "type": "string",
                        "description": "Period: 'this_month', 'last_month', 'last_30_days' or 'YYYY-MM'",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Custom start date (ISO). Overrides period.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Custom end date (ISO). If omitted with start_date, defaults to today.",
                    },
                },
                "required": ["account_id", "period"],
            },
        ),
        Tool(
            name="analyze_transfers",
            description="Analyze transfers between accounts and currency exchanges. Answers: 'Where did I transfer money?', 'Currency exchanges'",
            inputSchema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Period: 'this_month', 'last_month', 'last_30_days' or 'YYYY-MM'",
                        "default": "this_month",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Number of top transfers to return",
                        "default": 15,
                    },
                },
            },
        ),
        Tool(
            name="detect_anomalies",
            description="Detect anomalous spending (outliers, suspicious duplicates). Answers: 'Any unusual spending?', 'Suspicious transactions?'. Severity: z>=3.0 high, z>=2.0 medium, else low. Minimum z_threshold is 1.5.",
            inputSchema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Period: 'this_month', 'last_month', 'last_30_days' or 'YYYY-MM'",
                        "default": "this_month",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Custom start date (ISO). Overrides period.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Custom end date (ISO). If omitted with start_date, defaults to today.",
                    },
                    "category_id": {
                        "type": "string",
                        "description": "Category UUID to filter",
                    },
                    "z_threshold": {
                        "type": "number",
                        "description": "Z-score threshold for outlier detection (minimum 1.5)",
                        "default": 2.0,
                    },
                },
            },
        ),
        Tool(
            name="get_debts",
            description="Get debt summary: who owes whom. Answers: 'My debts?', 'Who owes me?'",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="suggest_category",
            description="Suggest a category for a transaction via ZenMoney API. Answers: 'What category for McDonalds?'",
            inputSchema={
                "type": "object",
                "properties": {
                    "payee": {
                        "type": "string",
                        "description": "Payee/merchant name or description",
                    },
                },
                "required": ["payee"],
            },
        ),
        Tool(
            name="convert_currency",
            description="Convert amount between currencies using real ZenMoney exchange rates. Answers: 'How much is 100 USD in EUR?'",
            inputSchema={
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "number",
                        "description": "Amount to convert",
                    },
                    "from_currency": {
                        "type": "string",
                        "description": "Source currency code (USD, EUR, PLN, BYN, RUB, RON, CZK, HUF, GBP, etc.)",
                    },
                    "to_currency": {
                        "type": "string",
                        "description": "Target currency code",
                    },
                },
                "required": ["amount", "from_currency", "to_currency"],
            },
        ),
        Tool(
            name="get_exchange_rates",
            description="Get current exchange rates with cross-rate table. Defaults to currencies from your accounts. Use for any currency rate questions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "currencies": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of currency codes (e.g. ['USD', 'EUR', 'PLN']). If omitted, uses currencies from your accounts.",
                    },
                },
            },
        ),
        Tool(
            name="search_transactions",
            description="Search transactions by various criteria: date, category, account, amount, payee.",
            inputSchema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Period: 'this_month', 'last_month', 'last_30_days' or 'YYYY-MM'",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Custom start date (ISO). Overrides period.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Custom end date (ISO). If omitted with start_date, defaults to today.",
                    },
                    "category_id": {
                        "type": "string",
                        "description": "Category UUID (includes subcategories)",
                    },
                    "account_id": {
                        "type": "string",
                        "description": "Account UUID",
                    },
                    "category_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "maxItems": 100,
                        "uniqueItems": True,
                        "description": "Category IDs; descendants are included",
                    },
                    "account_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "maxItems": 100,
                        "uniqueItems": True,
                        "description": "Account IDs matched on either transaction side",
                    },
                    "category_state": {
                        "type": "string",
                        "enum": ["any", "categorized", "uncategorized"],
                        "default": "any",
                        "description": "Filter transactions by category presence",
                    },
                    "merchant_id": {
                        "type": "string",
                        "description": "Merchant UUID",
                    },
                    "payee_search": {
                        "type": "string",
                        "description": "Search by payee, comment, or merchant name",
                    },
                    "min_amount": {
                        "type": "number",
                        "description": "Minimum amount",
                    },
                    "max_amount": {
                        "type": "number",
                        "description": "Maximum amount",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["income", "outcome", "transfer"],
                        "description": "Transaction type",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results",
                        "default": 50,
                    },
                    "cursor": {
                        "type": "string",
                        "maxLength": 2048,
                        "description": "Opaque cursor from the previous page",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["date", "amount"],
                        "default": "date",
                    },
                    "sort_order": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "desc",
                    },
                },
                "additionalProperties": False,
            },
        ),
    ] + _planning_tools() + _decision_tools() + _entity_read_tools()
        + _mutation_tools())
    if not remote:
        return tools
    tools = [
        tool for tool in tools if tool.name not in REMOTE_EXCLUDED_TOOLS
    ] + [
        Tool(
            name="force_sync",
            description="Request an immediate asynchronous ZenMoney synchronization.",
            inputSchema={
                "type": "object",
                "properties": {
                    "force_full": {
                        "type": "boolean",
                        "default": False,
                        "description": "Request a full snapshot instead of an incremental sync",
                    }
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="get_sync_status",
            description="Get the current sync request state and last successful cache sync time.",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
    ]
    return [
        tool.model_copy(
            update={
                "annotations": ToolAnnotations(
                    readOnlyHint=tool.name
                    not in {
                        "force_sync",
                        *PREPARE_TOOL_ENTITIES,
                        "prepare_changes",
                        "prepare_mixed_changes",
                        "prepare_recurring_payment",
                        "apply_changes",
                    },
                    destructiveHint=tool.name == "apply_changes",
                    openWorldHint=tool.name
                    in {"force_sync", "apply_changes"},
                )
            }
        )
        for tool in tools
    ]


def _public_sync_state(state: dict[str, Any]) -> dict[str, Any]:
    force_full = state["force_full"]
    if force_full is None:
        mode = None
    elif force_full:
        mode = "full"
    else:
        mode = "incremental"
    return {
        "state": state["state"],
        "request_id": state["request_id"],
        "mode": mode,
        "requested_at": state["requested_at"],
        "started_at": state["started_at"],
        "finished_at": state["finished_at"],
        "failure_code": state["failure_code"],
    }


def _text_result(result: dict[str, Any]) -> list[TextContent]:
    return [
        TextContent(
            type="text", text=json.dumps(result, ensure_ascii=False, indent=2)
        )
    ]


def _dispatch_entity_read_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    db: HardenedDatabase,
) -> list[TextContent]:
    try:
        if name in LIST_ENTITY_TOOLS:
            if set(arguments) - {"limit", "cursor", "include_inactive"}:
                raise EntityResourceError("entity_tool_arguments_invalid")
            result = list_entity_resource(
                db,
                LIST_ENTITY_TOOLS[name],
                limit=arguments.get("limit", 50),
                cursor=arguments.get("cursor"),
                include_inactive=arguments.get("include_inactive", False),
            )
        else:
            entity_type = GET_ENTITY_TOOLS[name]
            identity_name = "key" if entity_type == "budget" else "id"
            if set(arguments) != {identity_name}:
                raise EntityResourceError("entity_tool_arguments_invalid")
            result = get_entity_resource(
                db, entity_type, arguments[identity_name]
            )
    except EntityResourceError:
        raise MCPError(INVALID_PARAMS, "Invalid tool arguments") from None
    return _text_result(result)


def _dispatch_remote_control_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    control_path: Path,
    db: HardenedDatabase | None,
    db_path: str | Path | None,
) -> list[TextContent]:
    if name == "force_sync":
        if set(arguments) - {"force_full"} or type(
            arguments.get("force_full", False)
        ) is not bool:
            raise MCPError(INVALID_PARAMS, "Invalid tool arguments")
        try:
            state = request_sync(
                control_path, force_full=arguments.get("force_full", False)
            )
        except InvalidSyncState:
            return _text_result(
                {"status": "rejected", "failure_code": "invalid_sync_state"}
            )
        result = {
            "status": state["status"],
            "request_id": state["request_id"],
            "mode": "full" if state["force_full"] else "incremental",
            "requested_at": state["requested_at"],
        }
        return _text_result(result)

    if arguments:
        raise MCPError(INVALID_PARAMS, "Invalid tool arguments")
    try:
        state = _public_sync_state(read_sync_state(control_path))
    except InvalidSyncState:
        state = {
            "state": "failed",
            "request_id": None,
            "mode": None,
            "requested_at": None,
            "started_at": None,
            "finished_at": None,
            "failure_code": "invalid_sync_state",
        }

    cache_status = {
        "last_server_timestamp": 0,
        "last_sync_time": None,
        "cache_stats": {},
        "staleness": "never_synced",
    }
    owned_db = False
    status_db = db
    if status_db is None and db_path is not None and Path(db_path).is_file():
        status_db = HardenedDatabase(db_path, read_only=True)
        owned_db = True
    try:
        if status_db is not None:
            cache_status = get_sync_status_resource(status_db)
    finally:
        if owned_db:
            status_db.close()
    return _text_result({**state, **cache_status})


async def _dispatch_mutation_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    db: HardenedDatabase,
    remote: bool,
    mutation_path: Path,
) -> list[TextContent]:
    store = ProposalStore(mutation_path)
    try:
        if name == "prepare_recurring_payment":
            try:
                result = prepare_recurring_payment(db, store, arguments)
            except MutationStateError:
                result = {"status": "rejected", "failure_code": "mutation_not_ready"}
            except MutationValidationError:
                result = {"status": "rejected", "failure_code": "invalid_changes"}
            return _text_result(result)

        if name in PREPARE_TOOL_ENTITIES or name in {
            "prepare_changes", "prepare_mixed_changes"
        }:
            if set(arguments) != {"operations"}:
                raise MCPError(INVALID_PARAMS, "Invalid tool arguments")
            try:
                result = prepare_changes(
                    db,
                    store,
                    arguments["operations"],
                    entity_type=PREPARE_TOOL_ENTITIES.get(name),
                )
            except MutationStateError:
                result = {"status": "rejected", "failure_code": "mutation_not_ready"}
            except MutationValidationError:
                result = {
                    "status": "rejected",
                    "failure_code": "invalid_changes",
                }
            return _text_result(result)

        if set(arguments) != {"proposal_id"} or not isinstance(
            arguments.get("proposal_id"), str
        ):
            raise MCPError(INVALID_PARAMS, "Invalid tool arguments")
        proposal_id = arguments["proposal_id"]
        try:
            if name == "get_change_proposal":
                result = get_change_proposal(store, proposal_id)
            elif remote:
                result = store.request_apply(proposal_id)
            else:
                result = await execute_proposal(
                    db, get_sync_engine(), store, proposal_id
                )
        except MutationStateError:
            result = {"status": "rejected", "failure_code": "proposal_not_found"}
        return _text_result(result)
    finally:
        store.close()


async def _dispatch_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    db: HardenedDatabase,
) -> list[TextContent]:
    """Handle tool calls."""
    if name in ENTITY_READ_TOOLS:
        return _dispatch_entity_read_tool(name, arguments, db=db)

    if name == "sync_data":
        engine = get_sync_engine()
        force_full = arguments.get("force_full", False)
        result = await engine.sync(force_full=force_full)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "get_net_worth":
        result = get_net_worth(db)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "get_liquidity":
        result = get_liquidity(
            db,
            target_amount=arguments.get("target_amount"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "analyze_spending":
        result = analyze_spending(
            db,
            period=arguments.get("period", "this_month"),
            category_id=arguments.get("category_id"),
            top_n=arguments.get("top_n", 10),
            include_transfers=arguments.get("include_transfers", False),
            include_holds=arguments.get("include_holds", False),
            start_date=arguments.get("start_date"),
            end_date=arguments.get("end_date"),
            group_by=arguments.get("group_by", "category"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "analyze_income":
        result = analyze_income(
            db,
            period=arguments.get("period", "this_month"),
            top_n=arguments.get("top_n", 10),
            start_date=arguments.get("start_date"),
            end_date=arguments.get("end_date"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "analyze_merchants":
        result = analyze_merchants(
            db,
            period=arguments.get("period", "this_month"),
            category_id=arguments.get("category_id"),
            top_n=arguments.get("top_n", 10),
            start_date=arguments.get("start_date"),
            end_date=arguments.get("end_date"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "check_budget_health":
        result = check_budget_health(
            db,
            month=arguments.get("month"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "get_upcoming_payments":
        result = get_upcoming_payments(
            db,
            days_ahead=arguments.get("days_ahead", 30),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "analyze_trends":
        result = analyze_trends(
            db,
            months=arguments.get("months", 6),
            category_id=arguments.get("category_id"),
            metric=arguments.get("metric", "outcome"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "detect_recurring":
        result = detect_recurring(
            db,
            lookback_months=arguments.get("lookback_months", 3),
            tolerance_pct=arguments.get("tolerance_pct", 10),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "get_account_flow":
        result = get_account_flow(
            db,
            account_id=arguments.get("account_id"),
            period=arguments.get("period"),
            start_date=arguments.get("start_date"),
            end_date=arguments.get("end_date"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "analyze_transfers":
        result = analyze_transfers(
            db,
            period=arguments.get("period", "this_month"),
            top_n=arguments.get("top_n", 15),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "detect_anomalies":
        result = detect_anomalies(
            db,
            period=arguments.get("period", "this_month"),
            category_id=arguments.get("category_id"),
            z_threshold=arguments.get("z_threshold", 2.0),
            start_date=arguments.get("start_date"),
            end_date=arguments.get("end_date"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "get_debts":
        result = get_debts(db)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "suggest_category":
        engine = get_sync_engine()
        result = await suggest_category(
            payee=arguments.get("payee"),
            token=engine.token,
            db=db,
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "convert_currency":
        result = convert_currency(
            db,
            amount=arguments.get("amount"),
            from_currency=arguments.get("from_currency"),
            to_currency=arguments.get("to_currency"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "get_exchange_rates":
        result = get_exchange_rates(
            db,
            currencies=arguments.get("currencies"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "search_transactions":
        result = search_transactions(
            db,
            period=arguments.get("period"),
            category_id=arguments.get("category_id"),
            account_id=arguments.get("account_id"),
            merchant_id=arguments.get("merchant_id"),
            payee_search=arguments.get("payee_search"),
            min_amount=arguments.get("min_amount"),
            max_amount=arguments.get("max_amount"),
            tx_type=arguments.get("type"),
            limit=arguments.get("limit", 50),
            start_date=arguments.get("start_date"),
            end_date=arguments.get("end_date"),
            cursor=arguments.get("cursor"),
            sort_by=arguments.get("sort_by", "date"),
            sort_order=arguments.get("sort_order", "desc"),
            category_state=arguments.get("category_state", "any"),
            category_ids=arguments.get("category_ids"),
            account_ids=arguments.get("account_ids"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "get_financial_snapshot":
        result = get_financial_snapshot(db)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "get_cash_flow":
        result = get_cash_flow(
            db,
            period=arguments.get("period", "current_period"),
            start_date=arguments.get("start_date"),
            end_date=arguments.get("end_date"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "get_spending_baseline":
        result = get_spending_baseline(
            db,
            months=arguments.get("months", 6),
            category_id=arguments.get("category_id"),
            include_current_partial_month=arguments.get(
                "include_current_partial_month", False
            ),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "compare_periods":
        result = compare_periods(
            db,
            preset=arguments.get("preset", "last_month_vs_previous"),
            period_a=arguments.get("period_a"),
            period_b=arguments.get("period_b"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "get_emergency_fund_status":
        result = get_emergency_fund_status(
            db,
            essential_category_ids=arguments.get("essential_category_ids"),
            monthly_essential_override=arguments.get("monthly_essential_override"),
            baseline_months=arguments.get("baseline_months", 6),
            target_months=arguments.get("target_months", 6),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "get_debt_service":
        result = get_debt_service(db, arguments.get("obligation_overrides"))
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "get_financial_position":
        result = get_financial_position(
            db, arguments.get("obligation_overrides")
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "forecast_cash_flow":
        result = forecast_cash_flow(
            db, horizon_days=arguments.get("horizon_days", 90)
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "plan_emergency_fund":
        result = plan_emergency_fund(
            db,
            target_months=arguments.get("target_months", 6),
            essential_category_ids=arguments.get("essential_category_ids"),
            monthly_essential_override=arguments.get("monthly_essential_override"),
            minimum_liquidity_buffer=arguments.get("minimum_liquidity_buffer", 0),
            allocation_pct_of_free_cash_flow=arguments.get("allocation_pct_of_free_cash_flow", 75),
            include_restricted_deposits=arguments.get("include_restricted_deposits", False),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "plan_debt_payoff":
        result = plan_debt_payoff(
            db,
            monthly_extra_payment=arguments.get("monthly_extra_payment", 0),
            strategy=arguments.get("strategy", "avalanche"),
            debt_accounts=arguments.get("debt_accounts"),
            custom_order=arguments.get("custom_order"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "compare_debt_strategies":
        result = compare_debt_strategies(
            db,
            monthly_extra_payment=arguments.get("monthly_extra_payment"),
            debt_accounts=arguments.get("debt_accounts"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "plan_financial_goal":
        result = plan_financial_goal(
            db,
            name=arguments.get("name"),
            target_amount=arguments.get("target_amount"),
            current_amount=arguments.get("current_amount", 0),
            target_date=arguments.get("target_date"),
            monthly_contribution=arguments.get("monthly_contribution"),
            priority=arguments.get("priority", "medium"),
            annual_return_pct=arguments.get("annual_return_pct", 0),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "plan_multiple_goals":
        result = plan_multiple_goals(
            monthly_available=arguments.get("monthly_available"),
            goals=arguments.get("goals"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "run_financial_scenario":
        result = run_financial_scenario(
            db,
            horizon_months=arguments.get("horizon_months"),
            scenario=arguments.get("scenario"),
            scenario_name=arguments.get("scenario_name", "base"),
            minimum_liquidity_buffer=arguments.get("minimum_liquidity_buffer", 0),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    elif name == "build_financial_plan":
        result = build_financial_plan(
            db,
            planning_horizon_months=arguments.get("planning_horizon_months", 24),
            minimum_liquidity_buffer=arguments.get("minimum_liquidity_buffer", 100_000),
            emergency_fund=arguments.get("emergency_fund"),
            debt_accounts=arguments.get("debt_accounts"),
            goals=arguments.get("goals"),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    else:
        raise ValueError(f"Unknown tool: {name}")


async def call_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    db: HardenedDatabase | None = None,
    remote: bool = False,
    db_path: str | Path | None = None,
    control_path: str | Path | None = None,
    mutation_path: str | Path | None = None,
) -> list[TextContent]:
    """Dispatch a tool with the appropriate local or remote database lifecycle."""
    if remote and name not in {tool.name for tool in await list_tools(remote=True)}:
        raise MCPError(INVALID_PARAMS, "Remote tool is unavailable")

    if remote and name in REMOTE_CONTROL_TOOLS:
        return _dispatch_remote_control_tool(
            name,
            arguments,
            control_path=(
                Path(control_path) if control_path is not None else DEFAULT_CONTROL_PATH
            ),
            db=db,
            db_path=db_path,
        )

    owned_db = remote and db is None
    if db is None:
        if remote and db_path is not None:
            db = HardenedDatabase(db_path, read_only=True)
        else:
            db = open_remote_db() if remote else get_db()

    try:
        if name in MUTATION_TOOLS:
            if mutation_path is None:
                if remote:
                    sync_path = (
                        Path(control_path)
                        if control_path is not None
                        else DEFAULT_CONTROL_PATH
                    )
                    resolved_mutation_path = sync_path.with_name(
                        "mutation-proposals.db"
                    )
                else:
                    resolved_mutation_path = get_mutation_path()
            else:
                resolved_mutation_path = Path(mutation_path)
            return await _dispatch_mutation_tool(
                name,
                arguments,
                db=db,
                remote=remote,
                mutation_path=resolved_mutation_path,
            )
        return await _dispatch_tool(name, arguments, db=db)
    finally:
        if owned_db:
            db.close()


# ============================================================================
# Resources
# ============================================================================

async def list_resources() -> list[Resource]:
    """List available resources."""
    entity_resources = [
        Resource(
            uri=f"zenmoney://{name}",
            name=name.replace("-", " ").title(),
            description=f"Paginated ZenMoney {entity_type} entities",
            mimeType="application/json",
        )
        for name, entity_type in ENTITY_RESOURCE_NAMES.items()
    ]
    return entity_resources + [
        Resource(
            uri="zenmoney://categories",
            name="Categories",
            description="Expense and income category tree",
            mimeType="application/json",
        ),
        Resource(
            uri="zenmoney://budgets/current",
            name="Budgets",
            description="Budget limits for the current month",
            mimeType="application/json",
        ),
        Resource(
            uri="zenmoney://instruments",
            name="Currencies",
            description="Currency reference with exchange rates",
            mimeType="application/json",
        ),
        Resource(
            uri="zenmoney://sync-status",
            name="Sync Status",
            description="Sync state and cache statistics",
            mimeType="application/json",
        ),
        Resource(
            uri="zenmoney://financial-snapshot",
            name="Financial Snapshot",
            description="Current financial snapshot from the synchronized local cache",
            mimeType="application/json",
        ),
    ]


async def list_resource_templates() -> list[ResourceTemplate]:
    """List collection and exact user-entity URI templates."""
    templates: list[ResourceTemplate] = []
    for name, entity_type in ENTITY_RESOURCE_NAMES.items():
        templates.append(
            ResourceTemplate(
                name=f"{name}_collection",
                uriTemplate=(
                    f"zenmoney://{name}{{?limit,cursor,include_inactive}}"
                ),
                description=f"Paginated ZenMoney {entity_type} entities",
                mimeType="application/json",
            )
        )
        exact = (
            f"zenmoney://{name}/{{owner_user_id}}/{{date}}/{{tag_key}}"
            if entity_type == "budget"
            else f"zenmoney://{name}/{{id}}"
        )
        templates.append(
            ResourceTemplate(
                name=f"{name}_exact",
                uriTemplate=exact,
                description=f"One exact ZenMoney {entity_type} entity",
                mimeType="application/json",
            )
        )
    return templates


async def _dispatch_resource(
    uri: str, *, db: HardenedDatabase
) -> str:
    """Read resource content."""
    if uri == "zenmoney://categories":
        result = get_categories_resource(db)
    elif uri == "zenmoney://budgets/current":
        result = get_current_budgets_resource(db)
    elif uri == "zenmoney://instruments":
        result = get_instruments_resource(db)
    elif uri == "zenmoney://sync-status":
        result = get_sync_status_resource(db)
    elif uri == "zenmoney://financial-snapshot":
        result = get_financial_snapshot(db)
    else:
        parsed = urlparse(uri)
        entity_type = ENTITY_RESOURCE_NAMES.get(parsed.netloc)
        if (
            parsed.scheme != "zenmoney"
            or entity_type is None
            or parsed.fragment
            or parsed.params
        ):
            raise EntityResourceError("entity_resource_uri_invalid")

        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(values) != 1 for values in query.values()):
            raise EntityResourceError("entity_resource_uri_invalid")
        path = [unquote(part) for part in parsed.path.split("/") if part]
        if not path:
            if set(query) - {"limit", "cursor", "include_inactive"}:
                raise EntityResourceError("entity_resource_uri_invalid")
            try:
                limit = int(query.get("limit", ["50"])[0])
            except ValueError as exc:
                raise EntityResourceError("limit_invalid") from exc
            include_value = query.get("include_inactive", ["false"])[0]
            if include_value not in {"true", "false"}:
                raise EntityResourceError("include_inactive_invalid")
            result = list_entity_resource(
                db,
                entity_type,
                limit=limit,
                cursor=query.get("cursor", [None])[0],
                include_inactive=include_value == "true",
            )
        else:
            if query:
                raise EntityResourceError("entity_resource_uri_invalid")
            if entity_type == "budget":
                if len(path) != 3:
                    raise EntityResourceError("entity_resource_uri_invalid")
                try:
                    owner_user_id = int(path[0])
                except ValueError as exc:
                    raise EntityResourceError("entity_key_invalid") from exc
                key: Any = {
                    "owner_user_id": owner_user_id,
                    "date": path[1],
                    "tag": None if path[2] == "null" else path[2],
                }
            else:
                if len(path) != 1:
                    raise EntityResourceError("entity_resource_uri_invalid")
                key = path[0]
            result = get_entity_resource(db, entity_type, key)

    return json.dumps(result, ensure_ascii=False, indent=2)


async def read_resource(
    uri: str,
    *,
    db: HardenedDatabase | None = None,
    remote: bool = False,
    db_path: str | Path | None = None,
) -> str:
    """Read a resource with the appropriate local or remote database lifecycle."""
    uri = str(uri)
    owned_db = remote and db is None
    if db is None:
        if remote and db_path is not None:
            db = HardenedDatabase(db_path, read_only=True)
        else:
            db = open_remote_db() if remote else get_db()

    try:
        try:
            return await _dispatch_resource(uri, db=db)
        except EntityResourceError as exc:
            if remote:
                raise MCPError(
                    INVALID_PARAMS, "Remote resource is unavailable"
                ) from None
            raise
    finally:
        if owned_db:
            db.close()


# ============================================================================
# Main
# ============================================================================

def create_server(
    *,
    remote: bool = False,
    db_path: str | Path | None = None,
    control_path: str | Path | None = None,
    mutation_path: str | Path | None = None,
) -> Server:
    """Create an MCP SDK v2 server backed by the shared registry."""

    async def _on_list_tools(context, params):
        return ListToolsResult(tools=await list_tools(remote=remote))

    async def _on_call_tool(context, params):
        try:
            content = await call_tool(
                params.name,
                dict(params.arguments or {}),
                remote=remote,
                db_path=db_path,
                control_path=control_path,
                mutation_path=mutation_path,
            )
        except MCPError:
            raise
        except Exception as exc:
            if not remote:
                raise
            LOGGER.warning(
                json.dumps(
                    {
                        "event": "remote_tool_call",
                        "tool": params.name,
                        "status": "failed",
                        "exception_class": type(exc).__name__,
                    }
                )
            )
            raise MCPError(INTERNAL_ERROR, "Remote tool failed") from None
        return CallToolResult(content=content)

    async def _on_list_resources(context, params):
        return ListResourcesResult(resources=await list_resources())

    async def _on_list_resource_templates(context, params):
        return ListResourceTemplatesResult(
            resourceTemplates=await list_resource_templates()
        )

    async def _on_read_resource(context, params):
        try:
            text = await read_resource(
                params.uri,
                remote=remote,
                db_path=db_path,
            )
        except MCPError:
            raise
        except Exception as exc:
            if not remote:
                raise
            LOGGER.warning(
                json.dumps(
                    {
                        "event": "remote_resource_read",
                        "status": "failed",
                        "exception_class": type(exc).__name__,
                    }
                )
            )
            raise MCPError(INTERNAL_ERROR, "Remote resource failed") from None
        return ReadResourceResult(
            contents=[
                TextResourceContents(
                    uri=params.uri,
                    mime_type="application/json",
                    text=text,
                )
            ]
        )

    return Server(
        name="zenmoney-mcp",
        version=__version__,
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
        on_list_resources=_on_list_resources,
        on_list_resource_templates=_on_list_resource_templates,
        on_read_resource=_on_read_resource,
    )

def main() -> None:
    """Run the MCP server."""
    import asyncio

    from mcp.server.stdio import stdio_server

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            server = create_server()
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
