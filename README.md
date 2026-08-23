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

The server also exposes resources for accounts, categories, budgets, merchants,
currencies, and synchronization status.

## Hardening in this fork

The installed `zenmoney-mcp` command runs `zenmoney_mcp.entrypoint`, which keeps
the upstream server intact and installs a small runtime overlay:

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

```bash
git clone https://github.com/ekho/zenmoney-mcp.git ~/zenmoney-mcp
cd ~/zenmoney-mcp
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Set the token in the process environment rather than committing it:

```bash
export ZENMONEY_TOKEN="replace-with-your-token"
```

The first hardened start performs additive SQLite migrations. Back up
`~/.cache/zenmoney-mcp/zenmoney.db` before the first run when preserving an
existing cache matters. A full sync can recreate the cache from ZenMoney.

## ChatGPT Desktop and Codex

Use the absolute executable path in `~/.codex/config.toml`:

```toml
[mcp_servers.zenmoney]
command = "/Users/you/zenmoney-mcp/.venv/bin/zenmoney-mcp"
env_vars = ["ZENMONEY_TOKEN"]
tool_timeout_sec = 120
```

Restart the desktop client after changing MCP configuration. This repository is
a local stdio server; ChatGPT web needs a supported remote-MCP or secure-tunnel
setup instead of a local executable.

## Claude Desktop or Cursor

```json
{
  "mcpServers": {
    "zenmoney": {
      "command": "/Users/you/zenmoney-mcp/.venv/bin/zenmoney-mcp",
      "env": {
        "ZENMONEY_TOKEN": "replace-with-your-token"
      }
    }
  }
}
```

## Data flow

1. `sync_data` reads `/v8/diff/` into a local SQLite cache at
   `~/.cache/zenmoney-mcp/zenmoney.db`.
2. Analytics run locally against SQLite.
3. Only sync and category suggestion make read-only requests to ZenMoney.

## Testing

```bash
python -m pip install -e ".[dev]"
python -m compileall -q src tests
python -m pytest tests/ -v --ignore=tests/test_integration.py
```

The live integration test requires `ZENMONEY_TOKEN` and is excluded from CI.

## License

MIT
