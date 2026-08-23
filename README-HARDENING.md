# Financial correctness hardening

This fork keeps the original ZenMoney MCP read-only model and adds a hardened runtime focused on trustworthy financial analytics.

## Runtime

The installed `zenmoney-mcp` command now starts `zenmoney_mcp.entrypoint`. The entrypoint loads the original MCP server, then installs:

- `HardenedDatabase` for schema migration and strict exchange-rate handling;
- `HardenedSyncEngine` for validated, atomic synchronization;
- corrected analytics for net worth, liquidity, budgets, debts, account flow, spending, transaction search, and FX;
- bounded validation wrappers around the remaining legacy analytics.

The original modules remain available, but the package command uses the hardened runtime by default.

## Financial semantics

### Net worth

- `net_worth` includes active accounts where `in_balance=true`.
- `net_worth_all_accounts` also includes active accounts excluded from the ZenMoney balance.
- Excluded accounts are returned separately instead of silently changing the primary total.

### Liquidity

- Own liquid funds, accessible savings, term/restricted deposits, and credit availability are separate values.
- Credit is borrowing capacity, not an asset.
- Restricted savings are not assumed to be immediately spendable.

### Budgets

- The user's configured budget-month start day is respected.
- Parent category budgets include descendants.
- Planned marker tags are read as JSON arrays.
- Marker amounts use explicit marker currency when present, then account currency as fallback.
- Spending in categories without a budget row is returned as `unbudgeted_spending`.
- A zero budget with actual spending is never reported as `on_track`.

### Debts

- Debt-account balances are authoritative.
- Transaction history is used for counterparty attribution.
- Any mismatch is exposed as a reconciliation gap rather than hidden.

### Account flow

- Native account currency and converted user-currency amounts are returned together.
- Transfers contribute to signed account movement and net change.
- Holds are excluded by default.

### Search and FX

- Search limits and aggregation bounds are rejected when invalid; they are not silently clamped.
- Candidate transactions with missing or zero exchange rates fail explicitly.
- FX results identify their source as synchronized ZenMoney instrument data and do not claim a specific upstream provider.

## Synchronization guarantees

- A response must contain an integer `serverTimestamp` and array-shaped entity fields.
- Incremental and full synchronization are applied to a staging database first.
- The live cache is replaced only after validation and all upserts succeed.
- Full synchronization replaces the cache, preventing stale rows from surviving a fresh snapshot.

## Verification

```bash
python -m pip install -e ".[dev]"
python -m compileall -q src tests
python -m pytest tests/ -v --ignore=tests/test_integration.py
```

The live integration test still requires `ZENMONEY_TOKEN` and is intentionally excluded from CI.
