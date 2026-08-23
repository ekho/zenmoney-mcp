# Financial Planning Analytics Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add seven read-only, deterministic planning analytics tools and one cache-only MCP resource on top of the Phase 1 financial-correctness runtime.

**Architecture:** Put budget-period resolution in `periods.py`, strict primary-currency conversion in `money.py`, and all derived metrics in `planning.py`. Keep `server.py` as transport-only registration/dispatch and compose existing Phase 1 primitives instead of copying their account SQL.

**Tech Stack:** Python 3.11+, `sqlite3`, `datetime`, `calendar`, `statistics`, MCP SDK, pytest.

**Spec:** `docs/superpowers/specs/2026-08-23-financial-planning-analytics-phase-2-design.md`

## Global Constraints

- Read only from the synchronized SQLite cache; never call sync or ZenMoney network APIs.
- Do not add pandas, numpy, an ORM, or another dependency.
- Every non-zero converted amount requires a present positive instrument rate; never use a `1:1` fallback.
- Exclude deleted rows, holds, household-external accounts, and transfers from household income/spending.
- Use the user's `monthStartDay`; exclude the current incomplete period from baselines and complete-period presets.
- Do not infer essential categories, APR, minimum payments, loan terms, or forecast certainty.
- Add no Phase 3–5 behavior.

---

### Task 1: Budget periods and strict money primitives

**Files:**
- Create: `src/zenmoney_mcp/periods.py`
- Create: `src/zenmoney_mcp/money.py`
- Create: `tests/test_planning.py`

**Interfaces:**
- Produces: `Period(start: date, end: date, label: str, complete: bool)`.
- Produces: `current_period(db, as_of)`, `completed_periods(db, count, as_of)`, `resolve_period(db, preset, start_date, end_date, as_of)`, and `comparison_periods(db, preset, as_of)`.
- Produces: `CurrencyContext(instrument_id, code, symbol, rate)`, `user_currency(db)`, and `convert(db, amount, instrument_id, target)`.

- [ ] **Step 1: Write failing period and FX tests**

```python
def test_completed_periods_respect_month_start_day(planning_db):
    planning_db.connect().execute("UPDATE users SET month_start_day=8")
    periods = completed_periods(planning_db, 2, date(2026, 8, 23))
    assert [(p.start.isoformat(), p.end.isoformat()) for p in periods] == [
        ("2026-06-08", "2026-07-07"),
        ("2026-07-08", "2026-08-07"),
    ]

def test_convert_rejects_missing_rate(planning_db):
    planning_db.connect().execute("UPDATE instruments SET rate=NULL WHERE id=2")
    with pytest.raises(CurrencyRateError):
        convert(planning_db, 10, 2, user_currency(planning_db))
```

- [ ] **Step 2: Run the new tests and verify missing-module failures**

Run: `uv run --frozen --extra dev pytest tests/test_planning.py -q`
Expected: collection fails because `periods.py` and `money.py` do not exist.

- [ ] **Step 3: Implement the minimum primitives**

Use `calendar.monthrange`, `date`, and `timedelta`; validate ISO dates through `date.fromisoformat`. `convert` delegates positive-rate validation to `HardenedDatabase.require_instrument_rate` and rejects missing instruments for non-zero amounts.

- [ ] **Step 4: Run focused tests**

Run: `uv run --frozen --extra dev pytest tests/test_planning.py -q`
Expected: period and FX tests pass.

### Task 2: Cash flow, spending baseline, and comparison

**Files:**
- Create: `src/zenmoney_mcp/planning.py`
- Modify: `tests/test_planning.py`

**Interfaces:**
- Produces: `get_cash_flow(db, period="current_period", start_date=None, end_date=None, as_of=None) -> dict`.
- Produces: `get_spending_baseline(db, months=6, category_id=None, as_of=None) -> dict`.
- Produces: `compare_periods(db, preset="last_month_vs_previous", period_a=None, period_b=None, as_of=None) -> dict`.
- Internal: one bounded transaction query per requested range and `_data_quality(db, as_of)`.

- [ ] **Step 1: Add failing cash-flow tests**

Add literal fixtures for income, spending, a transfer, a hold, a custom range, missing income, multiple currencies, and `monthStartDay=8`. Assert exact totals, counts, ranges, and `None` savings rate.

- [ ] **Step 2: Verify the cash-flow tests fail because the functions are absent**

Run: `uv run --frozen --extra dev pytest tests/test_planning.py -q -k cash_flow`
Expected: import or attribute failure for `get_cash_flow`.

- [ ] **Step 3: Implement one-pass normalized cash flow**

Query eligible transactions for the inclusive range, convert only the applicable side, and aggregate direct primary tags in dictionaries. Return rounded values and compact data-quality metadata.

- [ ] **Step 4: Add and verify failing baseline/comparison tests**

Add 3/6/12-month, outlier-median, category-descendant, zero-base, new-category, decrease, and year-ago assertions. Run the focused tests and confirm the unimplemented outputs fail.

- [ ] **Step 5: Implement baseline and comparison using the cash-flow primitive**

Use `statistics.fmean`, `statistics.median`, and inclusive quartiles. Keep zero-spending completed periods. Build category deltas from the union of both period maps and return `None` percentages for zero bases.

- [ ] **Step 6: Run the focused module**

Run: `uv run --frozen --extra dev pytest tests/test_planning.py -q`
Expected: all Task 1–2 tests pass.

### Task 3: Emergency fund and debt service

**Files:**
- Modify: `src/zenmoney_mcp/planning.py`
- Modify: `tests/test_planning.py`

**Interfaces:**
- Produces: `get_emergency_fund_status(db, essential_category_ids=None, monthly_essential_override=None, baseline_months=6, target_months=6, as_of=None) -> dict`.
- Produces: `get_debt_service(db, as_of=None) -> dict`.

- [ ] **Step 1: Add failing emergency-fund tests**

Cover `configuration_required`, descendant category baseline, override, excluded restricted deposit, excluded credit, zero baseline, exact target, and above/below target with hand-derived totals.

- [ ] **Step 2: Verify red**

Run: `uv run --frozen --extra dev pytest tests/test_planning.py -q -k emergency`
Expected: missing-function failures.

- [ ] **Step 3: Implement emergency-fund composition**

Reuse Phase 1 `get_liquidity`; eligible reserve is `liquid_own + savings_accessible`. Validate mutually exclusive configurations and return structured configuration status instead of raising when neither is supplied.

- [ ] **Step 4: Add failing debt-service tests**

Cover a transfer payment into a negative loan/debt account, borrowing in the opposite direction, no debt, multiple accounts, FX conversion, and zero income.

- [ ] **Step 5: Implement debt-service aggregation**

Use active `loan` and `debt` account balances and one transfer query per selected period, not per account. Count only transfers entering a debt account from a non-debt household account. Return ratio `None` when income is zero.

- [ ] **Step 6: Run the focused module**

Run: `uv run --frozen --extra dev pytest tests/test_planning.py -q`
Expected: all Task 1–3 tests pass.

### Task 4: Forecast and financial snapshot

**Files:**
- Modify: `src/zenmoney_mcp/planning.py`
- Modify: `tests/test_planning.py`

**Interfaces:**
- Produces: `forecast_cash_flow(db, horizon_days=90, as_of=None) -> dict`.
- Produces: `get_financial_snapshot(db, as_of=None) -> dict`.

- [ ] **Step 1: Add failing forecast tests**

Cover planned income/outcome, 30/60/90 bounds, out-of-horizon markers, marker currency fallback, strict missing FX, no markers, recurring-name deduplication, and exact scenario balances.

- [ ] **Step 2: Verify red**

Run: `uv run --frozen --extra dev pytest tests/test_planning.py -q -k forecast`
Expected: missing-function failures.

- [ ] **Step 3: Implement scheduled and recurring scenario inputs**

Read planned markers in one bounded query. Detect monthly/weekly/biweekly/quarterly recurring expenses from completed history with stable amounts and intervals, normalize names for deduplication, and prorate unmatched monthly estimates to the horizon. Return assumptions and warnings.

- [ ] **Step 4: Add failing snapshot composition tests**

Cover excluded accounts, multiple currencies, positive/negative debts, empty cache, and exclusion of the current partial period. Assert that snapshot fields match direct primitive outputs.

- [ ] **Step 5: Implement snapshot composition**

Call Phase 1 `get_net_worth`, `get_liquidity`, and `get_debts`; call the new cash-flow, recurring, and scheduled-marker helpers. Do not copy their SQL or call MCP handlers.

- [ ] **Step 6: Run the focused module**

Run: `uv run --frozen --extra dev pytest tests/test_planning.py -q`
Expected: all planning tests pass.

### Task 5: MCP transport, resource, documentation, and scale smoke

**Files:**
- Modify: `src/zenmoney_mcp/server.py`
- Modify: `tests/test_entrypoint.py`
- Create: `tests/test_planning_mcp.py`
- Modify: `README.md`

**Interfaces:**
- Adds MCP tools `get_financial_snapshot`, `get_cash_flow`, `get_spending_baseline`, `compare_periods`, `get_emergency_fund_status`, `get_debt_service`, and `forecast_cash_flow`.
- Adds resource `zenmoney://financial-snapshot`.

- [ ] **Step 1: Add failing discovery, dispatch, resource, and schema tests**

Assert all names, enums, date patterns, numeric bounds, required nested custom-period fields, JSON dispatch results, and that resource reads the installed database without constructing a sync engine.

- [ ] **Step 2: Verify red**

Run: `uv run --frozen --extra dev pytest tests/test_planning_mcp.py -q`
Expected: missing tools/resource and dispatch failures.

- [ ] **Step 3: Register the tools and resource**

Import planning functions directly, append seven `Tool` declarations, add seven dispatch branches, and append/read the resource. Keep all functions synchronous and cache-only below the transport layer.

- [ ] **Step 4: Add the synthetic scale smoke**

Insert 50,000 eligible transactions with `executemany`, call a 12-month cash-flow aggregation, and assert the exact count and total without a wall-clock threshold.

- [ ] **Step 5: Update README**

Add the required question/tool table and limitations for essential-category configuration, unavailable APR, heuristic recurring detection, and scenario—not guaranteed—forecasting.

- [ ] **Step 6: Run all new tests and the existing suite**

Run: `uv run --frozen --extra dev pytest -q --ignore=tests/test_integration.py`
Expected: no failures.

### Task 6: Verification, review, and publication

**Files:**
- Modify only files required by verified Critical/Important findings.

**Interfaces:**
- Produces a clean committed branch, draft PR against `main`, and checked GitHub Actions result.

- [ ] **Step 1: Run required local gates**

```bash
uv run --frozen --extra dev python -m compileall -q src tests
uv run --frozen --extra dev pytest -q --ignore=tests/test_integration.py
uv run --frozen --extra dev python -c "import zenmoney_mcp.server"
uv run --frozen --extra dev python -c "import zenmoney_mcp.entrypoint"
git diff --check
```

- [ ] **Step 2: Audit the complete diff and repository contents**

Run `git diff --stat`, `git diff`, and searches for tokens, SQLite files, `.venv`, transfer double counting, zero denominators, unbounded queries, and network calls from planning code. Fix all Critical and Important findings through new failing tests.

- [ ] **Step 3: Commit with Conventional Commits and co-author trailer**

Use `feat: add financial planning analytics` and `Co-authored-by: OpenAI Codex <noreply@openai.com>` after fresh verification.

- [ ] **Step 4: Verify branch/upstream/remote before push**

Run `git branch --show-current`, `git status --short --branch`, `git remote -v`, `git rev-parse origin/main`, and `git push --dry-run origin HEAD:refs/heads/feature/financial-planning-analytics-phase-2`.

- [ ] **Step 5: Push and create the draft PR**

Push the explicit refspec, create a draft PR titled `feat: add financial planning analytics` against `main` with the required body, and do not merge.

- [ ] **Step 6: Check GitHub Actions to completion**

Use `gh run list --repo ekho/zenmoney-mcp --branch feature/financial-planning-analytics-phase-2`; on failure inspect `gh run view <id> --log-failed`, fix locally, republish, and recheck.
