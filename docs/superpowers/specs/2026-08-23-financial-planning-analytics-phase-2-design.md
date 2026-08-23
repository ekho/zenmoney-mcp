# Financial Planning Analytics Phase 2 Design

## Goal

Add a read-only planning analytics layer that returns deterministic, explainable, multi-currency-safe metrics from the synchronized ZenMoney SQLite cache. The layer exposes seven MCP tools and one cache-only resource; it does not make recommendations, modify ZenMoney, or trigger synchronization.

## Base and scope

Phase 2 starts from `origin/main` at merge commit `61ea04496080de5926267d4fbae82ca17a5310b1`, which contains Phase 1 PR #1. Phase 3 decision logic, writes, remote deployment, schedulers, notifications, dashboards, and investment or tax advice remain out of scope.

## Architecture

- `periods.py` owns budget-month boundaries, completed periods, rolling windows, presets, and strict custom date ranges.
- `money.py` owns primary-currency lookup and strict conversion. Every non-zero amount requires a valid positive synchronized instrument rate; there is no `1:1` fallback.
- `planning.py` owns derived planning metrics and composes Phase 1 `get_net_worth`, `get_liquidity`, and `get_debts` primitives for the snapshot.
- `server.py` only declares MCP schemas, dispatches calls, and exposes the resource.
- `analytics.py` is not extended.

The implementation uses `sqlite3`, `datetime`, `calendar`, and `statistics`; no dependency is added. Aggregations scan each selected transaction range once and do not issue per-transaction SQL.

## Period semantics

Budget months use the primary user's `monthStartDay`, clamped to the last valid day of short months. Date ranges are inclusive. `current_period` may be partial, while every baseline, trailing-complete-month preset, and comparison preset ends before the current budget period.

`get_cash_flow` supports the required presets plus a strict custom range supplied by both `start_date` and `end_date`. `compare_periods` accepts either one comparison preset or two objects containing required `start_date` and `end_date` values. Invalid or inverted ranges raise input-validation errors.

## Household and currency semantics

Household income is a non-deleted, non-hold transaction with `income > 0` and `outcome = 0`. Household spending is the inverse. A row with both sides positive is a transfer and contributes to neither. Transactions attached to an archived account or an account explicitly excluded from ZenMoney household balance are excluded.

Each side is converted using `amount * source_rate / user_rate`. Missing instruments or non-positive rates fail explicitly. Successful responses report an empty `missing_exchange_rates` list; calculations never return partial converted totals.

Category aggregation uses the first transaction tag, matching the existing analytics convention, and returns uncategorized activity explicitly. Essential-category filters include descendants and count a transaction once even if several requested categories overlap.

## Statistical semantics

Spending baseline uses completed budget months only. `median` is the primary metric, `mean` uses `statistics.fmean`, and p25/p75 use `statistics.quantiles(values, n=4, method="inclusive")`. The response names this percentile method. Percentage deltas are `null` when the comparison base is zero.

## Tool contracts

### `get_financial_snapshot`

Composes Phase 1 net worth, liquidity, and debt primitives with last-complete-month cash flow, trailing 3/12-month averages, recurring obligation estimate, planned 30-day markers, and data-quality metadata. It reports data only and performs no sync.

### `get_cash_flow`

Returns income, outcome, net cash flow, savings rate, and transaction counts for a preset or custom range. Savings rate is `null` when income is zero.

### `get_spending_baseline`

Accepts 3–24 months and an optional category. It returns monthly values and the requested descriptive statistics. Months with no matching spending remain zero-valued completed periods rather than disappearing.

### `compare_periods`

Supports the three required presets or two arbitrary ranges. It returns income, outcome, net cash flow, and unioned category deltas, including categories present in only one period.

### `get_emergency_fund_status`

Accepts exactly one of essential category IDs or a non-negative monthly override. With neither, it returns `configuration_required`. Eligible reserve is own liquid funds plus accessible savings; credit capacity and restricted deposits are excluded. Coverage is `null` when the essential baseline is zero because infinite coverage would be misleading.

### `get_debt_service`

Debt balance is the absolute value of negative active `loan` and `debt` account balances. A debt payment is a transfer whose income side enters one of those accounts from a non-debt household account; borrowing in the opposite direction is not a payment. The service ratio is payment divided by household income and is `null` when income is non-positive.

### `forecast_cash_flow`

Accepts only 30, 60, or 90 days. Planned reminder markers are high-confidence scheduled flows. Historical recurring outcomes are medium-confidence heuristics and are removed when their normalized payee matches a scheduled marker. The result exposes:

- `scheduled_only`: starting eligible liquid funds plus scheduled net;
- `scheduled_plus_recurring`: scheduled-only less unmatched recurring estimates prorated to the horizon;
- `baseline_spending`: starting funds plus scheduled income less the greater of scheduled outcome and the completed-month spending median prorated to the horizon.

The last rule treats scheduled outcomes as already represented in the historical baseline and prevents adding the same expected spending twice. Every response includes assumptions and warnings; no scenario is labeled as a prediction.

## Data quality

Every derived tool returns last sync time, staleness, completed months available, missing exchange rates, and warnings. Completed months available counts completed budget periods containing at least one eligible transaction through a whole-history SQL aggregate that returns one row; it does not materialize transactions or dates. No composite score is introduced.

## Resource

`zenmoney://financial-snapshot` calls the same planning function against the already-open cache. It never creates a sync engine, reads a token, or makes a network call.

## Validation and errors

MCP schemas use enums, numeric bounds, unique item constraints, ISO-date patterns, required nested fields, and descriptions. User configuration gaps return structured status values. Invalid arguments raise `InputValidationError`; missing cache reference data raises `FinancialDataError` or `CurrencyRateError`.

## Testing

Tests use a deterministic in-memory `HardenedDatabase` and fixed `as_of` dates. Each behavior is added red-first. Coverage includes all required financial, FX, period, zero-denominator, deduplication, and empty-cache cases, MCP discovery/dispatch/resource behavior, plus a 50,000-transaction synthetic smoke without a wall-clock assertion. The existing non-live suite, compile check, imports, and diff checks remain required.
