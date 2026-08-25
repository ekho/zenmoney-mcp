# ZenMoney MCP Server

Read-only MCP server for trustworthy personal-finance analytics over the
[ZenMoney](https://zenmoney.ru/) API. This fork keeps all data local, exposes no
write tools, and adds financially conservative calculations and atomic sync.

## Analytics

| Question | Tool |
|---|---|
| How much money do I have? | `get_net_worth` |
| Can I afford a purchase? | `get_liquidity` |
| Where does my money go? | `analyze_spending` |
| Where does my income come from? | `analyze_income` |
| Am I within budget? | `check_budget_health` |
| What subscriptions do I have? | `detect_recurring` |
| How are income and spending changing? | `analyze_trends` |
| What transfers did I make? | `analyze_transfers` |
| Are there unusual or duplicate expenses? | `detect_anomalies` |
| Who owes whom? | `get_debts` |
| What payments are coming up? | `get_upcoming_payments` |
| Find matching transactions | `search_transactions` |
| What happened on this account? | `get_account_flow` |
| Convert currencies | `convert_currency`, `get_exchange_rates` |
| Overall financial position | `get_financial_snapshot` |
| Monthly cash flow | `get_cash_flow` |
| Normal spending level | `get_spending_baseline` |
| Compare periods | `compare_periods` |
| Emergency fund coverage | `get_emergency_fund_status` |
| Debt burden | `get_debt_service` |
| 30/60/90 day forecast | `forecast_cash_flow` |

The server also exposes resources for accounts, categories, budgets, merchants,
currencies, synchronization status, and a cache-only financial snapshot at
`zenmoney://financial-snapshot`. Reading a resource never starts synchronization.

Planning analytics are deliberately conservative:

- emergency-fund coverage requires explicit essential category IDs or a monthly
  essential-spending override;
- debt service reports observed balances and payments but does not infer APR,
  minimum payments, or amortization schedules;
- recurring-payment detection is a historical heuristic and is labeled as such;
- cash-flow forecasts are transparent scenarios, not prediction guarantees.

## Financial Planning

Phase 3 adds deterministic decision support on top of the factual analytics.
Every result exposes inputs, assumptions, constraints, reasons, alternatives,
and measurable outcomes; it does not execute or write a financial decision.

| Question | Tool |
|---|---|
| How fast can I build a 6-month emergency fund? | `plan_emergency_fund` |
| Should I pay the high-interest loan first? | `plan_debt_payoff`, `compare_debt_strategies` |
| Can I afford a car in 18 months? | `plan_financial_goal` |
| Which of my goals conflict? | `plan_multiple_goals` |
| What happens if my income falls by 20%? | `run_financial_scenario` |
| How should I allocate my monthly free cash flow? | `build_financial_plan` |

Planning inputs that ZenMoney does not contain must be supplied explicitly:

```json
{
  "emergency_fund": {
    "target_months": 6,
    "essential_category_ids": ["category-id"]
  },
  "debt_accounts": {
    "loan-account-id": {
      "apr_pct": 19.9,
      "minimum_payment": 15000
    }
  },
  "goals": []
}
```

Missing APR, minimum payments, or essential-spending configuration returns
`configuration_required`; the server never invents those values. Calculations
use zero investment return, Decimal money arithmetic, and future calendar
month-end snapshots. Restricted deposits are excluded from emergency reserves
unless explicitly enabled, and credit capacity is always excluded.

See [`docs/planning-semantics.md`](docs/planning-semantics.md) for the priority
policy, formulas, rounding, data-quality labels, and limitations.

## Runtime modes

The installed `zenmoney-mcp` command is the local stdio server for Codex,
ChatGPT Desktop, Claude Desktop, and Cursor. It uses the shared SDK v2
registry and hardened runtime directly; it does not preserve an upstream
server through a runtime overlay.

For a private remote deployment, `zenmoney-mcp-http` exposes Streamable HTTP
at `/mcp` only inside Docker, and the OpenAI Secure MCP Tunnel client connects
outbound to OpenAI. The remote registry excludes the local API-dependent
`sync_data` and `suggest_category` tools. Its financial tools remain
read-only; remote-only `force_sync` can asynchronously request an
incremental or full cache refresh from the separate credentialed worker, and
`get_sync_status` reports progress and the last successful sync. The MCP
container still receives no ZenMoney token and cannot write the financial
snapshot directly. See the [remote operations runbook](deploy/remote-mcp/README.md)
and [threat model](docs/remote-mcp-threat-model.md).

## Hardening in this fork

The installed `zenmoney-mcp` command runs `zenmoney_mcp.entrypoint` against a
shared SDK v2 registry with hardened database, synchronization, and analytics
implementations:

- `HardenedDatabase` adds idempotent migrations and strict FX handling;
- `HardenedSyncEngine` validates responses and atomically replaces the live cache;
- corrected analytics cover net worth, liquidity, budgets, debts, account flow,
  spending, transaction search, upcoming payments, and FX;
- remaining analytics receive bounded runtime validation;
- MCP discovery advertises the same limits enforced at runtime.

Key semantics:

- `net_worth` includes only active accounts with `in_balance=true`;
- excluded accounts are returned separately in `net_worth_all_accounts`;
- credit is borrowing capacity, not an asset;
- accessible savings and term deposits are not treated as equivalent liquidity;
- budget periods respect the user's configured month-start day;
- zero-budget and unbudgeted spending are surfaced explicitly;
- debt-account balances are authoritative and attribution gaps remain visible;
- account flow includes signed transfers in native and user currency;
- missing or zero exchange rates fail explicitly instead of becoming a 1:1 rate;
- full sync replaces the cache, preventing stale rows from surviving.

More detail is available in
[`README-HARDENING.md`](README-HARDENING.md).

## Installation

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run
the server directly from GitHub:

```bash
export ZENMONEY_TOKEN="replace-with-your-token"
uvx --from git+https://github.com/ekho/zenmoney-mcp.git zenmoney-mcp
```

`uvx` downloads the package into an isolated cached environment; cloning the
repository or creating a virtual environment is not required.

The first hardened start performs additive SQLite migrations. Back up
`~/.cache/zenmoney-mcp/zenmoney.db` before the first run when preserving an
existing cache matters. A full sync can recreate the cache from ZenMoney.

## ChatGPT Desktop and Codex

Add the server to `~/.codex/config.toml`:

```toml
[mcp_servers.zenmoney]
command = "uvx"
args = ["--from", "git+https://github.com/ekho/zenmoney-mcp.git", "zenmoney-mcp"]
env_vars = ["ZENMONEY_TOKEN"]
tool_timeout_sec = 120
```

Restart the desktop client after changing MCP configuration. For ChatGPT web,
use the private remote Streamable HTTP + Secure MCP Tunnel deployment in the
[operations runbook](deploy/remote-mcp/README.md), not a local executable.

## Claude Desktop or Cursor

```json
{
  "mcpServers": {
    "zenmoney": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/ekho/zenmoney-mcp.git",
        "zenmoney-mcp"
      ],
      "env": {
        "ZENMONEY_TOKEN": "replace-with-your-token"
      }
    }
  }
}
```

## Development

Clone the repository only when changing or testing the code locally:

```bash
git clone https://github.com/ekho/zenmoney-mcp.git ~/zenmoney-mcp
cd ~/zenmoney-mcp
uv sync --extra dev
```

## Data flow

1. Local `sync_data` reads `/v8/diff/` directly through the local sync engine.
2. In the remote deployment, the periodic worker reads `/v8/diff/`; remote
   `force_sync` only asks that credentialed worker to run immediately.
3. Both modes publish a SQLite cache at `~/.cache/zenmoney-mcp/zenmoney.db` or
   the configured `ZENMONEY_DB_PATH`; analytics read that cache locally.
4. Only sync and category suggestion make read-only requests to ZenMoney.

## Testing

```bash
uv sync --extra dev
uv run python -m compileall -q src tests
uv run python -m pytest tests/ -v --ignore=tests/test_integration.py
```

The live integration test requires `ZENMONEY_TOKEN` and is excluded from CI.

## License

MIT
