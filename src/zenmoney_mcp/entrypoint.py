"""Default runtime that installs Phase 1 hardening before MCP startup."""

from __future__ import annotations

from typing import Any

from . import financial_correctness as corrected
from .hardened_database import HardenedDatabase
from .hardened_sync import HardenedSyncEngine

_PATCHED_ANALYTICS = (
    "analyze_income",
    "analyze_merchants",
    "analyze_spending",
    "analyze_transfers",
    "analyze_trends",
    "check_budget_health",
    "convert_currency",
    "detect_anomalies",
    "detect_recurring",
    "get_account_flow",
    "get_debts",
    "get_exchange_rates",
    "get_liquidity",
    "get_net_worth",
    "get_upcoming_payments",
    "search_transactions",
)

_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
_MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
_PERIOD_PATTERN = (
    r"^(this_month|last_month|last_30_days|\d{4}-(0[1-9]|1[0-2]))$"
)
_CURRENCY_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{1,11}$"


def harden_tool_schemas(tools):
    """Make MCP discovery reflect the bounds enforced by hardened functions."""

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
        schema = getattr(tool, "inputSchema", None)
        if not isinstance(schema, dict):
            continue
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            continue
        if getattr(tool, "name", None) == "analyze_spending":
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
            elif name == "period":
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


def _install_hardened_tool_discovery(server_module: Any) -> None:
    original = getattr(server_module, "list_tools", None)
    mcp_server = getattr(server_module, "server", None)
    register = getattr(mcp_server, "list_tools", None)
    if not callable(original) or not callable(register):
        return

    async def hardened_list_tools():
        return harden_tool_schemas(await original())

    server_module.list_tools = register()(hardened_list_tools)


def install_hardening(server_module: Any, legacy_analytics: Any) -> None:
    """Patch the already-defined server globals used by ``call_tool``."""
    existing_db = getattr(server_module, "_db", None)
    close = getattr(existing_db, "close", None)
    if callable(close):
        close()

    corrected.configure_legacy_analytics(legacy_analytics)
    server_module.Database = HardenedDatabase
    server_module.SyncEngine = HardenedSyncEngine
    for name in _PATCHED_ANALYTICS:
        setattr(server_module, name, getattr(corrected, name))
    _install_hardened_tool_discovery(server_module)
    # Any instances created before patching must not survive the transition.
    existing_db = getattr(server_module, "_db", None)
    close = getattr(existing_db, "close", None)
    if callable(close):
        close()
    server_module._db = None
    server_module._sync_engine = None


def main() -> None:
    """Install the overlay and run the upstream stdio MCP server."""
    from . import analytics as legacy_analytics
    from . import server as server_module

    install_hardening(server_module, legacy_analytics)
    server_module.main()
