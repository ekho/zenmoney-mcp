# Financial Planning Semantics

## Boundary and data flow

The planning layer is deterministic decision support, not an autonomous
financial adviser. It reads the synchronized local cache through the factual
financial snapshot, cash-flow, spending-baseline, emergency-fund, debt-service,
and forecast primitives. It does not rescan transaction history, synchronize,
call a remote API, or write to ZenMoney.

The eight planning tools return structured inputs, assumptions, constraints,
reasons, alternatives, and outcomes. Missing personal inputs produce
`configuration_required` with field-level reasons. No confidence score is
calculated; planning outputs use the categorical `high`, `medium`, or `low`
data-quality vocabulary with explicit limitations.

Every planning `data_quality.last_sync` value is an RFC3339 UTC timestamp with
a `Z` suffix, or `null` when unavailable. The cache continues to store its sync
metadata as Unix epoch, and `last_server_timestamp` remains the separate numeric
ZenMoney delta cursor. At the MCP protocol boundary planning results, like every
tool result, are delivered as a native object in `structuredContent` and as the
identical JSON object in `TextContent` for compatibility; discovery uses the
generic output schema `{"type":"object"}`.

## Financial obligations

`get_debt_service` treats every active, non-archived account with a negative
balance as a current financial obligation. This rule is independent of
ZenMoney's `inBalance` flag: that flag still controls UI/net-worth inclusion,
but it cannot hide money the user owes.

Default classification is deterministic:

| ZenMoney account type | Classification | Confidence |
|---|---|---|
| `loan` | `loan` | `high` |
| `ccard` | `credit_card` | `high` |
| `debt` | `personal_debt` | `high` |
| any other type, including `checking` | `other` | `low` |

Names, payees, merchants, categories, and comments are never used to guess a
credit product. A caller may explicitly override `classification`,
`minimum_payment`, or `apr_pct` for a current obligation. Unknown payment
amounts, dates, and APR remain `null`; zero is returned only when zero was
actually supplied. The nearest future planned transfer into an obligation may
provide a medium-confidence payment estimate with source `reminder`. It is not
presented as a contractual bank minimum.

Debt payoff includes every active negative-balance obligation. The caller must
provide its product terms explicitly; the server never infers APR or payment
rules from a name, category, merchant, or transaction history. A user-only
liability absent from ZenMoney is accepted only as `arbitrary` with an explicit
positive balance.

## Unified financial position

`get_financial_position` deliberately ignores `inBalance` as an economic
boundary. It includes every positive balance on an active account as an asset
and every negative balance as a liability. Cash, checking, electronic money,
credit-card positive balances, and accessible non-deposit savings are liquid;
the remaining positive balances are restricted assets. Negative balances are
reported as loans, credit cards, installments, or personal debts; the `other`
classification is included in personal debts so the liability total stays
complete.

Net worth is `total_assets - total_liabilities`. Operating income, operating
expenses, cash debt service, and free cash flow after debt service are arithmetic
means over the three most recent complete ZenMoney budget periods. The output states
this basis explicitly and returns the same data-quality warnings used by the
cash-flow primitives.

## Cash-flow components

`get_cash_flow` classifies each posted, non-deleted transaction by its account
relationship instead of discarding every transfer:

| Transaction shape | Component(s) | Household cash effect |
|---|---|---|
| income to an eligible non-obligation account | `income` | positive |
| expense from an eligible non-obligation account | `operating_expense` | negative |
| expense funded by an obligation | `operating_expense`, `financing_inflow` | zero until repayment |
| transfer from an eligible asset to an obligation | `debt_service_outflow` | negative |
| transfer from an obligation to an eligible asset | `financing_inflow` | positive |
| transfer between ordinary eligible assets | `internal_transfer` | zero |
| transfer involving savings or a deposit | `asset_transfer` | zero |
| transfer between obligations | `internal_transfer` | zero |
| incomplete or contradictory relationship | `unknown` | excluded with a warning |

An eligible non-obligation account preserves the existing household boundary:
it is active and is not explicitly excluded with `inBalance=false`.
Obligations remain in scope regardless of `inBalance`. Debt service uses the
converted source-side amount; financing received into an asset uses the
converted destination-side amount. This preserves the actual cash amount in
cross-currency transfers.

```text
operating net cash flow = income - operating expenses
net cash flow after debt service = operating net cash flow
                                   + financing inflow
                                   - debt-service cash outflow
savings rate before debt service = operating net cash flow / income * 100
savings rate after debt service = net cash flow after debt service / income * 100
```

Both rates are `null` when income is zero. Spending baselines, category
comparisons, and the existing integrated planner continue to use operating
expenses, so debt repayment is not counted a second time as consumption.
Unlinked single-sided inflows remain income because the cache contains no
account relationship proving that they are borrowed funds. Structurally
uncertain rows are excluded from totals, counted in the `unknown` component,
and exposed through at most 50 compact `uncertain_transactions` entries.

## Default priority policy

`build_financial_plan` exposes this policy in every response:

1. Preserve the minimum liquidity buffer.
2. Cover essential upcoming obligations.
3. Reach the emergency-fund target.
4. Meet minimum debt payments.
5. Reduce expensive debt.
6. Fund high-priority short- and medium-term goals.
7. Leave remaining free cash flow unallocated until the user selects an objective.

Minimum debt payments are deducted from historical net cash flow before the
remaining amount is described as free cash flow. The remaining capacity is then
allocated once, sequentially. Liquidity-buffer contributions also reduce the
emergency-fund gap, preventing the same money from being allocated twice.

The policy is a visible constant in the planning layer. The current API does not
expose a policy override; a later profile layer can replace it without changing
the calculation engines.

## Money and dates

Planning calculations convert inputs to `Decimal`, round money to two decimal
places with half-up rounding, and serialize rounded JSON numbers. Debt interest
is rounded monthly. Required goal contributions round upward to the nearest
cent so rounding cannot leave a goal short.

Historical budget periods retain the user's ZenMoney budget-month convention.
Future planning uses calendar months and month-end snapshots. A calculation run
on 23 August treats 30 September as the first future contribution date; four
future contributions complete on 31 December. Dates earlier than that first
future month end cannot use deadline mode.

Investment return is zero in the current decision-support model. The optional
`annual_return_pct` goal field accepts only `0`.

## Emergency fund

Monthly essential spending comes from either explicit essential category IDs
(including descendants) or a non-negative monthly override. The two sources are
mutually exclusive. The target and coverage formulas are:

```text
target = monthly essential spending * target months
coverage months = eligible reserve / monthly essential spending
gap = max(0, target - eligible reserve)
monthly contribution = max(0, trailing 3-month net cash flow) * allocation pct
completion months = ceil(gap / monthly contribution)
```

Eligible reserve contains own liquid funds and accessible savings. Credit
capacity is never eligible. Restricted or term deposits are excluded unless
`include_restricted_deposits=true` is supplied explicitly. Zero essential
spending has undefined coverage; a positive gap with zero contribution capacity
has no estimated completion date.

## Debt amortization

Every active negative-balance obligation requires a matching `debt_accounts`
entry. Four explicit models are supported:

| Model | Required terms | Optional terms |
|---|---|---|
| `fixed_loan` | `apr_pct` and exactly one of `fixed_payment` or legacy `minimum_payment` | title or scenario balance for an existing obligation |
| `credit_card` | `apr_pct`, `minimum_payment` | `statement_balance`; paired grace payment and future due date |
| `installment` | one to 120 unique future dated payments | `apr_pct`, defaulting to zero |
| `arbitrary` | `minimum_payment`; also `balance` when absent from ZenMoney | title and `apr_pct`, defaulting to zero |

Product-specific fields are rejected on incompatible models. Statement balance
cannot exceed total card debt. Grace payment and due date are supplied together,
and a scheduled or grace date must be in the future and within the 120-month
horizon.

For each calendar month and liability:

```text
monthly rate = APR / 100 / 12
interest = opening balance * monthly rate
amount due = opening balance + interest
payment = minimum payment + strategy allocation, capped at amount due
principal = payment - interest
ending balance = max(0, opening balance + interest - payment)
```

A grace or installment payment due later in the current calendar month creates
a leading partial-month row dated at that month end. That row contains only the
explicitly dated payments: recurring minimums on unrelated liabilities, monthly
extra payment, and the next monthly interest charge begin in the following full
month. This leading partial row is outside the 120-full-month planning horizon,
so a result may contain at most 121 rows and counts that partial row in
`estimated_payoff_months`.

`minimum_only` pays only the model's fixed, minimum, grace, or scheduled amount.
`avalanche` applies the remaining monthly budget to highest APR, breaking ties by
smaller balance and then account ID. `snowball` applies it to smallest balance,
breaking ties by higher APR and then account ID. `custom` requires every active
liability exactly once in `custom_order`. Avalanche, snowball, and custom retain
released payments in the shared budget after a liability is repaid.

Exact final payments are capped at the remaining amount due. If payment does not
exceed interest and no account makes positive principal progress, the result
reports `negative_amortization` and no payoff date. Strategy comparison names
separate winners for lowest interest and shortest duration; it never declares a
strategy universally best. Schedules stop after 120 months and report
`payoff_horizon_exceeded` instead of returning an unbounded payload.

## Transaction search and split

`search_transactions` accepts an arbitrary ISO date range, single or array
category/account filters, `categorized` or `uncategorized` state, and stable
sorting by date or converted amount in either direction. Results are bounded to
1–200 rows. `next_cursor` is an opaque, versioned keyset cursor tied to the chosen
sort field and direction; callers must reuse all filters and must not inspect or
modify the cursor. `total_matching` counts the complete filtered set, not the
remaining rows after the cursor. Uncategorized means `tag IS NULL` or an empty
JSON category array.

Transaction split is a confirmed write, never a direct client-side sequence.
`prepare_transaction_changes` or preferred `prepare_changes` (with compatible
`prepare_mixed_changes` alias) expands one `split`
operation into an update of the source transaction and creates for the remaining
parts. Only a full, posted, undeleted one-sided income or outcome can be split;
transfers and holds are rejected. There must be 2–100 positive parts whose amounts
match the source exactly, with at most one positive `remainder`.

The source ID stays on the first part. New parts receive UUIDs and preserve the
source raw metadata, including creation time, bank IDs, original payee, MCC,
reminder marker, and unknown sync fields. Native and operation-currency amounts
are divided with `Decimal`; cumulative half-up rounding keeps each operation
amount non-negative and puts the exact residual into the last part. All split
items are sent in one Diff batch. A changed source rejects the proposal before
write, and applying a terminal proposal again does not resend it.

## Transaction anomaly signals

`detect_anomalies` evaluates posted one-sided expenses in the selected period
and, only to identify recurrence, up to 400 days of prior history before the
selected end date. Exact duplicates share a date, user-currency amount to the
cent, normalized merchant/payee, category, and outcome account. The next class
shares merchant/payee and amount to the cent within one day; near duplicates
share normalized merchant/payee and category within two days and 5% of amount.
Pairs use that precedence. Transaction time is unavailable in the cache, so
duplicate results explicitly report day precision.

Periodic groups use equal category and cent-rounded user-currency amount with
unique dates: monthly is 25–35 days and at least three events; quarterly is
80–100 days, semiannual 170–195, and annual 350–380, each with at least two.
Only recurrence groups touching the selected period are returned. Their IDs are
excluded from `unusually_large_one_off`, which contains only positive z-scores
above the requested threshold. The new collections and compatible `outliers`
and `possible_duplicates` aliases each return at most 15 entries; complete
counts and any truncation are in `summary`.

## Confirmed mixed changes and recurring payments

`prepare_changes` is the public mixed-entity prepare tool. It accepts the same
strict `operations[]` schema as the compatible `prepare_mixed_changes` alias.
Preparing resolves create references such as `{"ref":"new-reminder"}` to UUIDs.
After preflight synchronization and `changed` conflict checks, one immutable
proposal becomes one mixed `/v8/diff/` write request. A send failure is an
external ambiguity, not a retry signal: the proposal becomes `needs_review`
with `write_result_unknown`; applying a terminal proposal sends no new request.

`prepare_recurring_payment` is a prepare-only shortcut for one monthly expense:

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

All fields are required; only a positive amount and `monthly` frequency are
accepted. The ISO `start_date` day must equal `day_of_month`, `end_date` cannot
precede it, the Account must be active, and the Tag must have the same owner.
The resulting mixed proposal creates a monthly `Reminder` (`interval="month"`,
`step=1`, `points=[0]`) and its first planned `ReminderMarker` on `start_date`.
Both carry the same account/instrument, tag, payee, `notify`, and one-sided
expense (`income=0`, `outcome=amount`). Only `apply_changes` writes either one.

## Spending baseline and expense patterns

`get_spending_baseline` selects 3–24 completed budget months. With
`include_current_partial_month=true`, it appends the current period through the
calculation date as `complete=false` with `days_elapsed` and `days_total`.
`monthly_series` is the canonical series and `monthly` is its compatibility
alias. The partial row is excluded from mean, median, quartiles, min/max,
trimmed mean, and pattern detection.

`trimmed_mean` sorts completed monthly values, removes `floor(n * 0.10)` from
each tail, then applies `statistics.fmean`; it equals the ordinary mean when
that count is zero. `expense_patterns` groups completed one-sided operating
expenses by normalized merchant/payee and category after conversion to the user
currency. Classification is deterministic:

| Class | Events and every interval |
|---|---|
| `recurring_monthly` | at least 3; 25–35 days |
| `likely_quarterly` | at least 2; 80–100 days |
| `likely_semiannual` | at least 2; 170–195 days |
| `likely_annual` | at least 2; 350–380 days |
| `one_off` | exactly 1; `low` confidence |
| `unknown` | all other groups; `low` confidence |

Every periodic class also requires `(max amount - min amount) / mean amount <=
20%` and reports `medium` confidence. These are historical heuristics, not
future-payment predictions. Results include the method, counts by class, total
group count, and at most the 100 largest groups by total amount; truncation is
explicit through `patterns_truncated` and `pattern_summary.truncated`.

## Goals

A single goal accepts exactly one planning mode:

- deadline mode divides the remaining gap by future month-end contribution dates;
- contribution mode divides the gap by the explicit monthly contribution and
  returns the resulting month-end date.

An already funded goal has a zero gap even when current funds exceed the target.
Multiple goals use stable greedy allocation by ascending numeric priority; input
order breaks equal-priority ties. Each allocation is capped at the contribution
required for its deadline. Underfunded goals return deadline or capacity
alternatives. Goal balances never exceed their targets unless a future API adds
an explicit overfunding option.

## Scenario engine

The scenario engine applies explicit changes for 1–120 calendar months. It does
not sample probabilities or use Monte Carlo. Each month is calculated as:

```text
month-end cash = month-start cash
               + changed baseline income
               - changed baseline expenses
               - explicit one-time expenses
               - explicit extra debt payment
               - explicit goal contributions
```

Cash, debt principal, goal balances, and net worth remain separate. Extra debt
payments are capped at outstanding debt and reduce cash and debt equally, so
they do not change net worth. Goal contributions are capped at the target and
remain part of net worth. Income, baseline expenses, and one-time expenses do
change net worth. Negative cash is retained to expose a financing gap rather
than silently clamped to zero.

The scenario does not reforecast debt interest without a complete amortization
configuration; that limitation is returned whenever extra principal is modeled.
Scheduled forecasts cover only their supported short horizons, so the
scenario does not invent long-range reminders.

## Integrated plan and recommendations

The integrated plan composes the financial snapshot, 30-day forecast,
emergency-fund status, debt service, configured goals, and the decision engines.
Its `recommended_action` is always accompanied by `reason`, `tradeoffs`,
`assumptions`, and `alternatives`. Recommendations describe the first unresolved
priority under the default policy; they do not execute anything.

Planning results are normally `medium` quality because future capacity and
personal classifications are assumptions even when historical facts are
complete. Limitations identify manually supplied essential categories, APR,
minimum payments, goal deadlines, and deterministic scenario changes.

## Decision-support limitations

The current decision-support layer does not provide investment selection or
optimization, retirement, tax, insurance, Monte Carlo, a dashboard, remote
deployment, notifications, automatic execution, or any ZenMoney write operation.
Persistent planning profiles are also out of scope; configuration is supplied in
tool arguments.
