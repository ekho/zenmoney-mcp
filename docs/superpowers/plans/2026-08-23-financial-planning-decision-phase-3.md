# Financial Planning Decision Layer Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add seven deterministic, read-only financial planning tools on top of the merged Phase 2 analytics primitives.

**Architecture:** Keep `planning.py` as the Phase 2 facts layer. Add a `decision` package whose pure Decimal engines consume explicit inputs, while reserve/scenario/orchestration adapters compose Phase 2 snapshot, cash-flow, emergency-fund, debt-service, and forecast functions. `server.py` remains transport-only.

**Tech Stack:** Python 3.11+, stdlib `decimal`, `datetime`, `calendar`, MCP SDK, pytest.

**Spec:** User-provided “ZenMoney MCP — Phase 3: Financial Planning & Decision Layer” contract dated 2026-08-23; runtime semantics are recorded in `docs/planning-semantics.md`.

## Global Constraints

- Preserve the read-only ZenMoney security model; no write or execution tool.
- Consume Phase 2 facts instead of rescanning transaction history.
- Use Decimal internally and round monetary JSON output to two decimals.
- Use month-end snapshots and calendar months for future planning.
- Require missing APR, minimum payments, essential categories, and other personal assumptions explicitly.
- Limit horizons to 1–120 months and debt/goal collections to 50 items.
- Keep deterministic complexity at O(months * (debts + goals)); no solver, Monte Carlo, or new dependency.

---

### Task 1: Shared planning primitives and emergency reserve

**Files:**
- Create: `src/zenmoney_mcp/decision/__init__.py`
- Create: `src/zenmoney_mcp/decision/models.py`
- Create: `src/zenmoney_mcp/decision/reserve.py`
- Create: `tests/test_decision.py`

**Interfaces:**
- Produces: Decimal money conversion/rounding, calendar month-end helpers, structured configuration gaps, and `plan_emergency_fund(db, ...)`.
- Consumes: Phase 2 `get_financial_snapshot`, `get_cash_flow`, `get_spending_baseline`, and `get_emergency_fund_status`.

- [ ] Write reserve tests for funded/below-target/zero-capacity/liquidity/restricted/credit cases.
- [ ] Run focused tests and confirm missing-module failures.
- [ ] Implement only the tested Decimal/date primitives and reserve adapter.
- [ ] Run focused tests and confirm green.

### Task 2: Debt amortization and strategy comparison

**Files:**
- Create: `src/zenmoney_mcp/decision/debt.py`
- Modify: `tests/test_decision.py`

**Interfaces:**
- Produces: `plan_debt_payoff(db, monthly_extra_payment, strategy, debt_accounts, custom_order=None)` and `compare_debt_strategies(...)`.
- Consumes: Phase 2 `get_debt_service` account balances and explicit APR/minimum-payment configuration.

- [ ] Write failing one/multiple debt, avalanche/snowball/tie/zero-APR/final-payment/negative-amortization/no-debt tests.
- [ ] Run focused debt tests and confirm red.
- [ ] Implement monthly interest, minimum payments, deterministic priority, exact final payments, and bounded non-payoff detection.
- [ ] Run focused debt tests and confirm green.

### Task 3: Single and multiple goals

**Files:**
- Create: `src/zenmoney_mcp/decision/goals.py`
- Modify: `tests/test_decision.py`

**Interfaces:**
- Produces: `plan_financial_goal(db, ...)` and `plan_multiple_goals(monthly_available, goals, as_of=None)`.
- Consumes: Phase 2 trailing cash-flow capacity for single-goal feasibility; explicit capacity for multiple goals.

- [ ] Write failing deadline/contribution/funded/impossible/date/priority/conflict tests.
- [ ] Run focused goal tests and confirm red.
- [ ] Implement zero-return calendar-month planning and stable greedy allocation by priority.
- [ ] Run focused goal tests and confirm green.

### Task 4: Deterministic scenarios and integrated plan

**Files:**
- Create: `src/zenmoney_mcp/decision/scenarios.py`
- Create: `src/zenmoney_mcp/decision/plan.py`
- Modify: `tests/test_decision.py`

**Interfaces:**
- Produces: `run_financial_scenario(db, horizon_months, scenario, ...)` and `build_financial_plan(db, ...)`.
- Consumes: Phase 2 snapshot, debt service, forecast, reserve facts, and Phase 3 goal/debt engines.

- [ ] Write failing baseline/shock/one-time/debt/liquidity/horizon/invariant tests.
- [ ] Run focused scenario tests and confirm red.
- [ ] Implement separate cash/debt/goal month-end state and warnings.
- [ ] Write failing integrated-priority/no-double-allocation/configuration tests.
- [ ] Implement the visible sequential default priority policy and structured recommendation contract.
- [ ] Run all decision tests and confirm green.

### Task 5: MCP schemas and dispatch

**Files:**
- Modify: `src/zenmoney_mcp/server.py`
- Create: `tests/test_decision_mcp.py`

**Interfaces:**
- Adds: `plan_emergency_fund`, `plan_debt_payoff`, `compare_debt_strategies`, `plan_financial_goal`, `plan_multiple_goals`, `run_financial_scenario`, and `build_financial_plan`.

- [ ] Write failing discovery/schema/dispatch/registration tests.
- [ ] Confirm missing tools and routes fail.
- [ ] Add strict schemas and direct cache-only dispatch.
- [ ] Run MCP and entrypoint tests.

### Task 6: Documentation, review, and publication

**Files:**
- Modify: `README.md`
- Create: `docs/planning-semantics.md`
- Modify only files required by verified review findings.

- [ ] Document policy, calculations, data quality, rounding, month-end convention, and limitations.
- [ ] Run compile, full non-live pytest, import, tool-list, and diff checks.
- [ ] Audit numeric boundaries, allocations, configuration gaps, secrets, and generated files.
- [ ] Request independent code review; fix Critical/Important findings with red-green tests.
- [ ] Create logical Conventional Commits with `Co-authored-by: OpenAI Codex <noreply@openai.com>`.
- [ ] Verify branch/upstream/remote and explicit dry-run refspec before push.
- [ ] Push `HEAD:refs/heads/feature/financial-planning-phase-3`, create the required draft PR against `main`, and wait for CI.
