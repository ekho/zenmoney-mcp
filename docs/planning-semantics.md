# Financial Planning Semantics

## Boundary and data flow

The planning layer is deterministic decision support, not an autonomous
financial adviser. It reads the synchronized local cache through the Phase 2
financial snapshot, cash-flow, spending-baseline, emergency-fund, debt-service,
and forecast primitives. It does not rescan transaction history, synchronize,
call a remote API, or write to ZenMoney.

The seven planning tools return structured inputs, assumptions, constraints,
reasons, alternatives, and outcomes. Missing personal inputs produce
`configuration_required` with field-level reasons. No confidence score is
calculated; planning outputs use the categorical `high`, `medium`, or `low`
data-quality vocabulary with explicit limitations.

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

The policy is a visible constant in the planning layer. Phase 3 does not expose
a policy override; a later profile layer can replace it without changing the
calculation engines.

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

Investment return is zero throughout Phase 3. The optional
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

APR and minimum payment are required for every active debt account. They are
never inferred from transactions. For each calendar month and account:

```text
monthly rate = APR / 100 / 12
interest = opening balance * monthly rate
amount due = opening balance + interest
payment = minimum payment + strategy allocation, capped at amount due
principal = payment - interest
ending balance = max(0, opening balance + interest - payment)
```

`minimum_only` pays only each active minimum. `avalanche` applies the remaining
fixed monthly budget to highest APR, breaking ties by smaller balance and then
account ID. `snowball` applies it to smallest balance, breaking ties by higher
APR and then account ID. `custom` requires every active account exactly once in
`custom_order`. Avalanche and snowball roll released payments into the remaining
fixed budget.

Exact final payments are capped at the remaining amount due. If payment does not
exceed interest and no account makes positive principal progress, the result
reports `negative_amortization` and no payoff date. Strategy comparison names
separate winners for lowest interest and shortest duration; it never declares a
strategy universally best. Schedules stop after 120 months and report
`payoff_horizon_exceeded` instead of returning an unbounded payload.

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
Phase 2 scheduled forecasts cover only their supported short horizons, so the
scenario does not invent long-range reminders.

## Integrated plan and recommendations

The integrated plan composes the financial snapshot, 30-day forecast,
emergency-fund status, debt service, configured goals, and the Phase 3 engines.
Its `recommended_action` is always accompanied by `reason`, `tradeoffs`,
`assumptions`, and `alternatives`. Recommendations describe the first unresolved
priority under the default policy; they do not execute anything.

Planning results are normally `medium` quality because future capacity and
personal classifications are assumptions even when historical facts are
complete. Limitations identify manually supplied essential categories, APR,
minimum payments, goal deadlines, and deterministic scenario changes.

## Phase 3 limitations

Phase 3 does not provide investment selection or optimization, retirement,
tax, insurance, Monte Carlo, a dashboard, remote deployment, notifications,
automatic execution, or any ZenMoney write operation. Persistent planning
profiles are also out of scope; configuration is supplied in tool arguments.
