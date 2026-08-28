# ZenMoney MCP Server

[![CI](https://github.com/ekho/zenmoney-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ekho/zenmoney-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/zenmoney-mcp-server.svg)](https://pypi.org/project/zenmoney-mcp-server/)
[![Python](https://img.shields.io/pypi/pyversions/zenmoney-mcp-server.svg)](https://pypi.org/project/zenmoney-mcp-server/)
[![Container](https://img.shields.io/badge/container-ghcr.io%2Fekho%2Fzenmoney--mcp-2496ED?logo=docker&logoColor=white)](https://github.com/ekho/zenmoney-mcp/pkgs/container/zenmoney-mcp)
[![License: MIT](https://img.shields.io/github/license/ekho/zenmoney-mcp.svg)](LICENSE)

[Документация на русском](README.ru.md)

MCP server for trustworthy personal-finance analytics and explicitly confirmed
user-entity changes over the [ZenMoney](https://zenmoney.ru/) API. The project
started as a fork of [nnslvp/zenmoney-mcp](https://github.com/nnslvp/zenmoney-mcp)
and is maintained here as a substantially extended version. It keeps its working
data local and adds financially conservative calculations, atomic sync, and a
two-step write workflow.

## Complete tool catalog

Both local and remote modes expose 59 tools. They share 57 tools and use two
mode-specific tools for synchronization and category suggestions.

| Area | Tools |
|---|---|
| Financial analytics | `get_net_worth`, `get_liquidity`, `analyze_spending`, `analyze_income`, `analyze_merchants`, `check_budget_health`, `get_upcoming_payments`, `analyze_trends`, `detect_recurring`, `get_account_flow`, `analyze_transfers`, `detect_anomalies`, `get_debts`, `convert_currency`, `get_exchange_rates`, `search_transactions` |
| Planning analytics | `get_financial_snapshot`, `get_financial_position`, `get_cash_flow`, `get_spending_baseline`, `compare_periods`, `get_emergency_fund_status`, `get_debt_service`, `forecast_cash_flow` |
| Decision support | `plan_emergency_fund`, `plan_debt_payoff`, `compare_debt_strategies`, `plan_financial_goal`, `plan_multiple_goals`, `run_financial_scenario`, `build_financial_plan` |
| Entity reads | `list_accounts`, `get_account`, `list_tags`, `get_tag`, `list_merchants`, `get_merchant`, `list_reminders`, `get_reminder`, `list_reminder_markers`, `get_reminder_marker`, `list_transactions`, `get_transaction`, `list_budgets`, `get_budget` |
| Confirmed entity changes | `prepare_account_changes`, `prepare_tag_changes`, `prepare_merchant_changes`, `prepare_reminder_changes`, `prepare_reminder_marker_changes`, `prepare_transaction_changes`, `prepare_budget_changes`, `prepare_changes`, `prepare_mixed_changes`, `prepare_recurring_payment`, `get_change_proposal`, `apply_changes` |

| Mode | Mode-specific tools |
|---|---|
| Local stdio | `sync_data`, `suggest_category` |
| Remote through OpenAI Secure MCP Tunnel | `force_sync`, `get_sync_status` |

## Analytics

| Question | Tool |
|---|---|
| How much money do I have? | `get_net_worth` |
| Can I afford a purchase? | `get_liquidity` |
| Where does my money go? | `analyze_spending` |
| Where does my income come from? | `analyze_income` |
| Which merchants receive my money? | `analyze_merchants` |
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
| Economic assets, liabilities, net worth, and free cash flow | `get_financial_position` |
| Legacy in-balance financial snapshot | `get_financial_snapshot` |
| Monthly cash flow | `get_cash_flow` |
| Normal spending level | `get_spending_baseline` |
| Compare periods | `compare_periods` |
| Emergency fund coverage | `get_emergency_fund_status` |
| Debt burden | `get_debt_service` |
| 30/60/90 day forecast | `forecast_cash_flow` |

The server also exposes paginated collection and exact resources for Account,
Tag, Merchant, Reminder, ReminderMarker, Transaction, and Budget, plus currencies,
synchronization status, and a cache-only financial snapshot at
`zenmoney://financial-snapshot`. Reading a resource never starts synchronization.

### Synchronization and anomaly contracts

Public synchronization timestamps (`requested_at`, `started_at`, `finished_at`,
`last_sync_time`, and planning `data_quality.last_sync`) are RFC3339 UTC strings
with a `Z` suffix; an unavailable value is `null`. The cache and control state
continue to store Unix-epoch integers. `last_server_timestamp` remains ZenMoney's
numeric delta cursor, not a synchronization timestamp.

`detect_anomalies` returns bounded `exact_duplicates` (same day,
converted amount to the cent, normalized merchant/payee, category, and outcome
account), `same_merchant_amount_close_timestamp` (same merchant/payee, exact
amount, and up to one day apart), `near_duplicates` (same normalized
merchant/payee and category, up to two days and 5% amount difference),
`periodic_recurrences`, and `unusually_large_one_off`. The cache has dates rather
than transaction times, so duplicate signals declare `timestamp_precision:
"day"`. Recurrence detection reads at most 400 days of history before the
selected period end, groups equal category and cent-rounded user-currency amounts
by unique dates, and returns only recurrences touching the selected period:
monthly 25–35 days (at least 3 events), quarterly 80–100, semiannual 170–195,
and annual 350–380 (at least 2 each). Periodic transaction IDs are excluded from
one-off outliers. Every new collection, plus the retained `outliers` and
`possible_duplicates` aliases, returns at most 15 results; `summary` contains
full counts and `results_truncated`.

MCP discovery declares the generic output schema `{"type":"object"}` for every
tool. At the protocol boundary each response contains the native object in
`structuredContent` and the identical JSON object as `TextContent` for clients
that still require the text fallback.

## Confirmed user-entity changes

All writes use two separate calls. Choose the entity-specific prepare tool for
ordinary work, or the preferred `prepare_changes` when one proposal creates or
changes several related entity types. `prepare_mixed_changes` is a compatible
alias with the same strict `operations[]` schema:

```text
prepare_account_changes        prepare_tag_changes
prepare_merchant_changes       prepare_reminder_changes
prepare_reminder_marker_changes
prepare_transaction_changes    prepare_budget_changes
prepare_changes                 prepare_mixed_changes (compatible alias)
prepare_recurring_payment
get_change_proposal            apply_changes
```

Prepare validates 1–100 operations and returns an immutable field-by-field
preview without writing to ZenMoney. After reviewing it, pass only its
`proposal_id` to `apply_changes`; `get_change_proposal` reports state and results.

Preparation requires a successful full sync so that untouched ZenMoney fields
can be preserved. Apply synchronizes and rejects the whole proposal before
writing if any source entity changed since preparation. `{"ref": "..."}` links
between creates are resolved while preparing, so one proposal is one mixed
`/v8/diff/` write request. This is the atomicity boundary: after a send failure,
the result is unknown, the proposal becomes `needs_review` with
`write_result_unknown`, and it is never retried automatically. Applying any
terminal proposal again does not send another write.

Create and update are supported for all seven user entities. Safe delete archives
an Account, marks a Transaction or ReminderMarker deleted, or clears a Budget.
Tag, Merchant, and Reminder deletion and all physical purge operations are not
exposed. Prepared proposals expire after 24 hours. Terminal proposals are retained
for 30 days, and an uncertain write or verification result becomes `needs_review`.

`prepare_transaction_changes` also supports a one-sided income or outcome split.
The first part keeps the source transaction ID; the remaining parts receive new
IDs. All parts are submitted in one Diff batch, preserve the source raw metadata,
and must add up exactly. Use one optional `remainder` part to avoid decimal drift:

```json
{
  "operations": [{
    "operation": "split",
    "transaction_id": "transaction-id",
    "parts": [
      {"amount": 730, "category_id": "groceries-category-id"},
      {"amount": "remainder", "category_id": "household-category-id"}
    ]
  }]
}
```

`prepare_recurring_payment` prepares one ordinary monthly expense and its first
planned occurrence without writing. Its exact payload is:

```json
{
  "name": "T-Bank credit card",
  "amount": 28060,
  "account_id": "account-id",
  "category_id": "category-id",
  "frequency": "monthly",
  "day_of_month": 18,
  "start_date": "2026-09-18",
  "end_date": null,
  "notify": true
}
```

Only positive amounts and `monthly` are supported. `start_date` must be an ISO
date whose day equals `day_of_month`; an optional `end_date` cannot precede it.
The account must be active and the category must belong to its owner. The result
is the ordinary mixed proposal containing a `Reminder` (`interval="month"`,
`step=1`, `points=[0]`) and its first planned `ReminderMarker` on `start_date`.
Both use the same account/instrument, category, `notify`, payee, and one-sided
expense (`income=0`, `outcome=amount`); only `apply_changes` can write them.

`get_spending_baseline` uses 3–24 completed budget months. With
`include_current_partial_month=true`, it appends the current period through the
calculation date to canonical `monthly_series`; `monthly` remains a compatible
alias. A partial row has `complete=false`, `days_elapsed`, and `days_total`, but
is excluded from all statistics and pattern detection. `trimmed_mean` sorts the
completed values and removes `floor(n * 10%)` from each tail before `fmean`.

`expense_patterns` groups completed one-sided operating expenses by normalized
merchant/payee and category, after conversion to the user currency. It reports
`recurring_monthly` (at least 3 events, 25–35-day intervals),
`likely_quarterly` (at least 2, 80–100), `likely_semiannual` (at least 2,
170–195), `likely_annual` (at least 2, 350–380), `one_off` (one event), or
`unknown`. Periodic classes also require amount spread no greater than 20% of
their mean. This is historical heuristic output, not a prediction: the response
includes class counts and totals, returns only the 100 largest groups, and marks
truncation explicitly.

Planning analytics are deliberately conservative:

- emergency-fund coverage requires explicit essential category IDs or a monthly
  essential-spending override;
- `get_cash_flow` separates operating expenses, financing inflows, and cash
  debt service;
- `get_debt_service` includes every active negative-balance account regardless
  of `inBalance`;
- `get_financial_position` applies that same economic boundary to every positive
  asset and negative liability, then reports three complete months of operating
  cash flow and debt service;
- unknown APR and payment terms remain `null` unless supplied explicitly;
- recurring-payment detection is a historical heuristic and is labeled as such;
- cash-flow forecasts are transparent scenarios, not prediction guarantees.

`search_transactions` supports stable cursor pagination, date or converted-amount
sorting, category/account arrays, and explicit category presence. To page through
all uncategorized expenses for an arbitrary period from largest to smallest, reuse
the returned `next_cursor` with the same filters:

```json
{
  "start_date": "2026-01-01",
  "end_date": "2026-08-31",
  "type": "outcome",
  "category_state": "uncategorized",
  "sort_by": "amount",
  "sort_order": "desc",
  "limit": 50
}
```

## Financial Planning

The server adds deterministic decision support on top of the factual analytics.
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

Debt payoff supports four explicit models. Existing negative-balance accounts
may use `fixed_loan`, `credit_card`, or `installment`; a liability absent from
ZenMoney must use `arbitrary` with an explicit balance. For example:

```json
{
  "strategy": "avalanche",
  "monthly_extra_payment": 5000,
  "debt_accounts": {
    "loan-account-id": {
      "liability_type": "fixed_loan",
      "apr_pct": 19.9,
      "fixed_payment": 15000
    },
    "card-account-id": {
      "liability_type": "credit_card",
      "apr_pct": 29.9,
      "minimum_payment": 5000,
      "statement_balance": 42000,
      "grace_period_payment": 42000,
      "grace_period_due_date": "2026-09-15"
    },
    "installment-account-id": {
      "liability_type": "installment",
      "payment_schedule": [{"date": "2026-09-30", "amount": 10000}]
    },
    "family-loan": {
      "liability_type": "arbitrary",
      "title": "Family loan",
      "balance": 50000,
      "minimum_payment": 5000
    }
  }
}
```

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
`sync_data` and `suggest_category` tools. Its analytical tools remain read-only.
Remote `force_sync` can request a cache refresh, while confirmed entity-change
proposals are queued for the separate credentialed worker. `get_sync_status`
and `get_change_proposal` report their respective progress. The MCP
container still receives no ZenMoney token and cannot write the financial
snapshot or call ZenMoney directly. See the
[remote operations runbook](deploy/remote-mcp/README.md) and
[threat model](docs/remote-mcp-threat-model.md).

`force_sync(force_full=false, wait_until_complete=false)` is asynchronous by
default. With `wait_until_complete=true`, it waits only for the same request ID,
polling the validated control state every 0.25 seconds for a fixed 60 seconds.
A terminal result is `completed` or `failed`; a timeout returns `status:
"timeout"`, the current `pending` or `running` state, and
`wait_timed_out: true` without cancelling the worker. Invalid or replaced state
fails closed with `invalid_sync_state`; a pending or running request remains
single-flight.

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
- active negative balances remain obligations regardless of `inBalance`;
- debt-account balances are authoritative and attribution gaps remain visible;
- account flow includes signed transfers in native and user currency;
- missing or zero exchange rates fail explicitly instead of becoming a 1:1 rate;
- full sync replaces the cache, preventing stale rows from surviving.

More detail is available in
[`README-HARDENING.md`](README-HARDENING.md).

## Local installation with uvx

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run
the server from PyPI:

Get a personal API token at [zerro.app/token](https://zerro.app/token), as
documented in the [official ZenMoney API wiki](https://github.com/zenmoney/ZenPlugins/wiki/ZenMoney-API).

```bash
export ZENMONEY_TOKEN="replace-with-your-token"
uvx --from zenmoney-mcp-server zenmoney-mcp
```

`uvx` downloads the package into an isolated cached environment; cloning the
repository or creating a virtual environment is not required.

To run the current `main` branch before its next PyPI release, use
`uvx --from git+https://github.com/ekho/zenmoney-mcp.git zenmoney-mcp`.

The first hardened start performs additive SQLite migrations. Back up
`~/.cache/zenmoney-mcp/zenmoney.db` before the first run when preserving an
existing cache matters. A full sync can recreate the cache from ZenMoney.

## Private ChatGPT installation with OpenAI Secure MCP Tunnel

ChatGPT web cannot start the local stdio command. For private remote access,
run the included Docker Compose deployment and connect it through
[OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
The MCP endpoint stays inside the Docker network; only the tunnel client makes
an outbound connection to OpenAI.

This mode requires Docker Engine with Compose v2, a ZenMoney token, an OpenAI
tunnel runtime API key, and a tunnel ID associated with the target ChatGPT
workspace. Clone the repository and create the non-secret environment file:

```bash
git clone https://github.com/ekho/zenmoney-mcp.git ~/zenmoney-mcp
cd ~/zenmoney-mcp
cp deploy/remote-mcp/.env.example deploy/remote-mcp/.env
```

Set `CONTROL_PLANE_TUNNEL_ID` in `deploy/remote-mcp/.env`. Provision the
ZenMoney token and OpenAI key as separate file-backed Compose secrets using the
ownership and permission commands in the
[remote operations runbook](deploy/remote-mcp/README.md); do not put either
secret in `.env`. Then pull and start the deployment:

```bash
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml pull
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml up -d --no-build --pull never
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml ps
```

Complete the health checks and `tunnel-client doctor` from the runbook, then add
the MCP app in ChatGPT Developer Mode with **Connection = Tunnel** and scan its
tools.

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
4. A local confirmed proposal is written synchronously. A remote confirmed
   proposal is persisted on the control volume and written by the worker.
5. Only the local process or credentialed worker can call ZenMoney.

## Testing

```bash
uv sync --extra dev
uv run python -m compileall -q src tests
uv run python -m pytest tests/ -v --ignore=tests/test_integration.py
```

The live integration test requires `ZENMONEY_TOKEN` and is excluded from CI.

## License

MIT
