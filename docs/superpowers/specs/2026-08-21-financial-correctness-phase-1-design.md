# ZenMoney MCP financial correctness — Phase 1 design

## Goal

Make the fork safe to use as the read-only data layer for a personal-finance agent. Phase 1 improves correctness of existing analytics and synchronization; it does not add portfolio advice, forecasting, or write operations.

## Constraints

- Preserve the original read-only ZenMoney integration.
- Keep SQLite and standard-library-oriented implementation; no dataframe or ORM dependency.
- Avoid a large fork of the original `server.py` and `analytics.py` where a small runtime overlay can preserve upstream mergeability.
- Fail explicitly when data is insufficient for a trustworthy calculation.
- Maintain compatibility with local stdio MCP clients such as ChatGPT Desktop and Codex.

## Architecture

The original package remains intact. A new `zenmoney_mcp.entrypoint` imports the original analytics and server modules, then replaces the server's runtime globals with hardened components before starting stdio transport.

Components:

1. `validation.py` — strict dates, periods, bounds, and amount ranges.
2. `hardened_database.py` — additive schema migrations and corrected upserts.
3. `hardened_sync.py` — response validation, staging, and atomic cache replacement.
4. `financial_correctness.py` — corrected financial calculations plus bounded wrappers around unchanged legacy analytics.
5. `entrypoint.py` — installs the overlay and starts the original server.

This design minimizes conflicts with upstream while allowing each corrected unit to be tested independently.

## Data model changes

- Add `accounts.start_balance`.
- Add `reminders.points`, `income_instrument`, and `outcome_instrument`.
- Add `reminder_markers.income_instrument` and `outcome_instrument`.
- Add normalized non-null `budgets.tag_key` and a unique index on `(user, date, tag_key)`.
- Store a schema version in `sync_meta`.

Migration is idempotent. Existing duplicate nullable budgets are collapsed to the most recently stored row before the unique index is created.

## Synchronization

Every diff response is validated before use. It must be an object with a valid integer `serverTimestamp`; entity and deletion fields must be arrays when present.

The response is applied to an in-memory staging database. Incremental sync starts from a copy of the live database. Full sync starts from an empty schema. Only after all operations succeed is the staging database backed up over the live cache. This gives atomic visibility and makes full sync a genuine replacement.

## Corrected analytics

- Net worth excludes `in_balance=false` from the primary total and reports an all-accounts alternative.
- Liquidity separates own cash, accessible savings, restricted deposits, and credit.
- Budget health respects custom budget periods, JSON category arrays, marker currency, unbudgeted categories, and zero-budget actuals.
- Debt balances are authoritative; attribution gaps are explicit.
- Account flow includes transfers and returns native and converted values.
- Spending rejects transfer mixing and directs the caller to transfer analysis.
- Search enforces a strict 1–200 row bound, converts amounts explicitly, and rejects unusable FX data.
- Currency conversion uses only positive synchronized rates and states the source conservatively.

## Error handling

Invalid client arguments raise `ValidationError`. Missing or zero exchange rates raise `CurrencyRateError`. Missing primary currency or instrument metadata raises `FinancialDataError`. Invalid synchronization responses and HTTP failures raise `SyncError` without mutating the live cache.

## Testing

Regression tests cover each audit-confirmed defect. Sync tests verify full replacement, incremental preservation, invalid-response immutability, and staging rollback. Database tests cover migration, nullable budget uniqueness, extended fields, and strict rates. Financial tests cover corrected totals and validation boundaries. CI runs the suite on Python 3.11, 3.12, and 3.13.

## Out of scope

- Changing transactions or categories in ZenMoney.
- Replacing recurring-payment and anomaly-detection heuristics.
- Cash-flow forecasting, emergency-fund calculations, debt payoff strategy, or investment planning.
- Remote HTTP/OAuth deployment for ChatGPT web.
