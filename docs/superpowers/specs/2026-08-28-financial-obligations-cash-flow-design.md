# Financial Obligations and Cash Flow Phase 1 Design

## Goal

Make the planning surface distinguish consumption, financing, and debt-service
cash movements. The result must identify every active negative-balance account
as a financial obligation, count transfers that repay those obligations, and
never report structurally linked borrowing as household income.

This is the first, P0 phase of the broader financial-planning change request.
It intentionally delivers the final response contract rather than temporary
compatibility aliases.

## Confirmed decisions

- Cash flow is cash-basis. Liability-funded spending contributes an operating
  expense and an equal financing inflow, so the purchase does not reduce cash a
  second time before repayment.
- Every active negative `checking` account is an obligation classified as
  `other` with low confidence unless the caller explicitly overrides its
  classification.
- Account-title, payee, merchant, and comment heuristics are not used to infer
  a liability or installment.
- Existing `get_cash_flow` and `get_debt_service` result fields are replaced by
  the final contract. Deprecated aliases are not returned.
- `inBalance` remains source/UI metadata and never removes an active liability
  from obligation totals.

## Scope

Phase 1 changes only the shared read-only planning logic, MCP schemas and
dispatch, and their tests. It does not persist financial overrides, change
ZenMoney entities, alter synchronization, extend the payoff planner, or add the
later `get_financial_position` tool.

The implementation remains in `planning.py` and `server.py`. A new domain
module, database table, migration, or dependency would add ownership without
improving this phase's result.

## Financial obligation model

`planning.py` gains one shared obligation collector. An account is a current
financial obligation when:

- it is not archived;
- its converted balance is negative; and
- it exists in the synchronized household account set.

All such accounts are included regardless of `inBalance`. Classification is
deterministic:

| Source account type | Default classification | Confidence |
| --- | --- | --- |
| `loan` | `loan` | high |
| `ccard` | `credit_card` | high |
| `debt` | `personal_debt` | high |
| `checking` | `other` | low |
| any other type | `other` | low |

The reported `balance` is the positive amount owed in the user's currency.
`source_account_type` and `in_balance` preserve the original account metadata.

Callers may pass an `obligation_overrides` object keyed by account ID. The only
supported override fields in this phase are `classification`,
`minimum_payment`, and `apr_pct`. An unknown account ID, an account that is not
a current obligation, an invalid classification, a negative amount, or an
invalid date fails validation rather than being silently ignored.

Each obligation has this final shape:

```json
{
  "account_id": "...",
  "title": "...",
  "classification": "loan|credit_card|installment|personal_debt|other",
  "classification_confidence": "high|medium|low",
  "balance": 0,
  "currency": "RUB",
  "source_account_type": "ccard",
  "in_balance": false,
  "minimum_payment": {
    "amount": null,
    "due_date": null,
    "source": "reminder|account|user_override|unknown",
    "confidence": "high|medium|low"
  },
  "apr_pct": {
    "value": null,
    "source": "account|user_override|unknown"
  }
}
```

Unknown amounts and dates are `null`, never zero. A caller override is reported
with source `user_override` and high confidence. Without an override, the
nearest future planned reminder-marker transfer from a non-obligation account
into the obligation may supply the amount and due date with source `reminder`
and medium confidence. This is explicitly a scheduled payment estimate, not a
claim about a contractual bank minimum. Undocumented raw account fields are not
interpreted as APR or payment terms in this phase.

## Flow classification

The existing transaction representation defines a transfer as a row with both
`income > 0` and `outcome > 0`; `outcome_account` is the source and
`income_account` is the destination. One range query loads settled,
non-deleted transactions and both account sides. Currency conversion continues
to fail explicitly when a required synchronized rate is missing.

The classifier produces economic components rather than forcing every
transaction into one mutually exclusive bucket. This is required for a credit
purchase, which is both consumption and financing.

| Transaction shape | Components | Cash effect |
| --- | --- | --- |
| single-sided income into a non-obligation account | `income` | positive |
| single-sided outcome from a non-obligation account | `operating_expense` | negative |
| single-sided outcome from an obligation | `operating_expense` plus `financing_inflow` | zero |
| transfer from non-obligation to obligation | `debt_service_outflow` | negative |
| transfer from obligation to non-obligation | `financing_inflow` | positive |
| transfer between ordinary asset accounts | `internal_transfer` | zero |
| transfer involving a savings/deposit asset but no obligation | `asset_transfer` | zero |
| transfer between obligations | `internal_transfer` | zero |
| structurally incomplete or contradictory row | `unknown` | excluded and warned |

For debt service, the source-side amount is authoritative because it is the
cash that left the household. For financing into an asset account, the
destination-side amount is authoritative because it is the cash received.
This also handles cross-currency transfers without assuming both sides have the
same numeric amount.

A single-sided inflow with no linked liability remains ordinary income because
the cache contains no factual account relationship proving that it is
borrowing. The response documents this structural limitation; linked transfers
from an obligation are classified as financing with high confidence.

## `get_cash_flow` contract

The tool keeps the existing period inputs and returns:

```json
{
  "period": {
    "preset": "last_complete_month",
    "start": "YYYY-MM-DD",
    "end": "YYYY-MM-DD",
    "complete": true
  },
  "currency": "RUB",
  "income": 0,
  "operating_expenses": 0,
  "operating_net_cash_flow": 0,
  "financing_inflow": 0,
  "debt_service_cash_outflow": 0,
  "net_cash_flow_after_debt_service": 0,
  "savings_rate_before_debt_service_pct": null,
  "savings_rate_after_debt_service_pct": null,
  "flow_components": {
    "income": {"amount": 0, "count": 0},
    "operating_expense": {"amount": 0, "count": 0},
    "internal_transfer": {"amount": 0, "count": 0},
    "financing_inflow": {"amount": 0, "count": 0},
    "debt_service_outflow": {"amount": 0, "count": 0},
    "asset_transfer": {"amount": 0, "count": 0},
    "unknown": {"amount": 0, "count": 0}
  },
  "uncertain_transactions": [],
  "data_quality": {}
}
```

The formulas are:

```text
operating_net_cash_flow = income - operating_expenses
net_cash_flow_after_debt_service =
    operating_net_cash_flow + financing_inflow - debt_service_cash_outflow
savings_rate_before_debt_service_pct = operating_net_cash_flow / income * 100
savings_rate_after_debt_service_pct = net_cash_flow_after_debt_service / income * 100
```

Both percentages are `null` when income is zero. Internal and asset transfers
do not affect either net measure. `uncertain_transactions` is bounded to 50
items and contains only transaction ID, proposed classification, reason, and
confidence; it does not duplicate the normal transaction-search response.

`operating_net_cash_flow` is the operating component, not by itself the change
in liquid account balances when consumption was financed by an obligation. In
that case the matching financing component offsets the expense. The final
`net_cash_flow_after_debt_service` value is the cash-basis household change.

## `get_debt_service` contract

The tool accepts optional `obligation_overrides` and returns the same shared
obligations plus payment facts:

```json
{
  "currency": "RUB",
  "total_liabilities": 0,
  "obligations": [],
  "last_complete_month": {
    "operating_income": 0,
    "debt_service_cash_outflow": 0,
    "debt_service_ratio_pct": null
  },
  "trailing_3_complete_months": {
    "average_debt_service_cash_outflow": 0
  },
  "data_quality": {}
}
```

The service ratio is debt-service cash outflow divided by operating income and
is `null` when income is zero. The same flow classifier used by
`get_cash_flow` supplies both monthly income and debt-service values, preventing
the two tools from drifting.

## MCP schema correction

`harden_tool_schemas` currently adds the legacy period regex to every property
named `period`, including properties that already declare a different enum.
The patcher will add the regex only when the property has no enum. Planning
period enums therefore remain the sole constraint, while legacy tools retain
their existing named-period/custom-month validation.

No new schema-validation dependency is needed. Contract tests inspect the
actual `tools/list` descriptors, assert that the planning period has no regex,
and exercise every documented enum value through the planning dispatcher.

## Error and data-quality behavior

- Invalid overrides raise `InputValidationError` with the failing field path.
- Missing or non-positive currency rates keep the existing fail-closed
  behavior.
- Unknown obligation terms remain `null` with source `unknown`.
- Structurally unknown transactions are not silently included in income or
  spending. They are summarized and produce a data-quality warning.
- No account title, merchant, payee, comment, or category text is treated as
  proof of borrowing or installment status.

## Tests

The implementation is written red-first and adds focused checks for:

- `loan`, negative `ccard`, and negative `checking` obligations, including
  `inBalance=false`;
- non-negative and archived accounts not appearing as current obligations;
- unknown terms returning `null` rather than invented zeroes;
- explicit classification, payment, and APR overrides;
- a planned reminder-marker payment estimate;
- a 100,000-unit asset-to-loan transfer producing no operating expense,
  100,000 debt-service cash outflow, and a 100,000 reduction after debt service;
- a 300,000-unit obligation-to-asset transfer producing financing inflow and no
  operating income;
- liability-funded spending producing equal operating-expense and financing
  components;
- cross-currency debt service using the actual source cash amount;
- every documented `get_cash_flow.period` enum value surviving the final MCP
  schema and dispatcher;
- removal of the old response fields from both final contracts.

The complete non-live suite remains the release gate.

## Later phases

The shared obligation collector and flow classifier are the only reusable
primitives intentionally created here. Later phases may consume them for the
payoff planner and `get_financial_position`; they do not justify speculative
tables, services, or configuration persistence in Phase 1.
