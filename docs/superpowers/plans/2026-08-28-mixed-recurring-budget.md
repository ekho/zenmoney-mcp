# Phase 3 Financial Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать и выпустить единый mixed proposal, ежемесячные плановые платежи и сезонную бюджетную аналитику.

**Architecture:** Расширяем существующие `ProposalStore`, `prepare_changes`, `execute_proposal` и `get_spending_baseline`. Cross-ref уже разрешаются при подготовке, поэтому executor формирует один mixed batch; high-level платёж компилируется в две существующие сущности; аналитика остаётся stdlib/SQLite.

**Tech Stack:** Python 3.11+, stdlib `datetime`/`statistics`, SQLite, MCP SDK 2.x, pytest.

**Spec:** `docs/superpowers/specs/2026-08-28-mixed-recurring-budget-design.md`

## Global Constraints

- Не добавлять зависимости, контейнеры, секреты, env-переменные или volume.
- Все write-входы проходят существующую нормализацию и двухшаговый proposal/apply.
- Один proposal отправляется ровно одним upstream `/v8/diff/` запросом.
- Partial month не участвует в baseline statistics и seasonality detection.
- Старые MCP-имена и поля остаются совместимыми.

---

### Task 1: Один атомарный mixed batch и публичный `prepare_changes`

**Files:**
- Modify: `src/zenmoney_mcp/mutations.py`
- Modify: `src/zenmoney_mcp/server.py`
- Test: `tests/test_mutations.py`
- Test: `tests/test_entrypoint.py`
- Test: `tests/test_remote_http.py`

**Interfaces:**
- Consumes: `normalize_operations(...)`, `rebuild_after(...)`, `HardenedSyncEngine.push_changes(changes)`.
- Produces: MCP tools `prepare_changes` и совместимый `prepare_mixed_changes`; один `dict[str, list[dict]]` на один вызов `push_changes`.

- [ ] **Step 1: Написать падающие tests**

Изменить dependency-create test так, чтобы Tag, Transaction и Reminder с ref
ожидались в одном `engine.pushed[0]`. В schema discovery потребовать одинаковую
strict schema у `prepare_changes` и `prepare_mixed_changes`; remote discovery
должен видеть оба имени с non-destructive annotations.

- [ ] **Step 2: Проверить RED**

Run: `uv run --extra dev pytest -q tests/test_mutations.py::test_cross_referenced_creates_are_sent_in_one_atomic_batch tests/test_entrypoint.py::test_change_tool_schemas_are_bounded_strict_and_entity_specific tests/test_remote_http.py::test_remote_mcp_exposes_truthfully_annotated_surface`

Expected: FAIL — dependency create отправляется несколькими batch и
`prepare_changes` отсутствует в discovery.

- [ ] **Step 3: Реализовать минимальный GREEN**

В `execute_proposal` убрать dependency levels и собрать один `outgoing` по
`DIFF_FIELDS`; вызвать `push_changes(outgoing)` один раз. В server добавить
`prepare_changes` как второе имя той же mixed schema/dispatch и включить его в
mutation annotations.

- [ ] **Step 4: Проверить GREEN**

Run: `uv run --extra dev pytest -q tests/test_mutations.py tests/test_entrypoint.py tests/test_remote_http.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zenmoney_mcp/mutations.py src/zenmoney_mcp/server.py \
  tests/test_mutations.py tests/test_entrypoint.py tests/test_remote_http.py
git commit -m "feat: apply mixed changes atomically"
```

### Task 2: High-level ежемесячный платёж

**Files:**
- Modify: `src/zenmoney_mcp/mutations.py`
- Modify: `src/zenmoney_mcp/server.py`
- Test: `tests/test_mutations.py`
- Test: `tests/test_entrypoint.py`

**Interfaces:**
- Consumes: `prepare_changes(db, store, operations, now=...)`.
- Produces: `prepare_recurring_payment(db, store, payment, now=None) -> dict` и MCP tool `prepare_recurring_payment`.

- [ ] **Step 1: Написать падающие tests**

Проверить proposal из двух create items. Reminder обязан иметь
`interval="month"`, `step=1`, `points=[0]`, account instrument, tag и payee;
ReminderMarker обязан ссылаться на UUID Reminder и иметь `state="planned"`.
Параметризованно отклонить archived/missing account, foreign/missing category,
не-monthly frequency, несовпадающий day и end before start. Schema должна быть
closed-world и содержать точные required/ranges.

- [ ] **Step 2: Проверить RED**

Run: `uv run --extra dev pytest -q tests/test_mutations.py -k recurring_payment tests/test_entrypoint.py -k recurring_payment`

Expected: FAIL — helper и tool ещё не существуют.

- [ ] **Step 3: Реализовать минимальный GREEN**

Добавить validator/compiler в `mutations.py`: прочитать raw Account/Tag,
проверить owner/instrument/dates и передать две mixed create operations в
существующий `prepare_changes`. В server зарегистрировать строгую schema и
направить вызов в helper; ошибки маппить в существующий `invalid_changes`.

- [ ] **Step 4: Проверить GREEN**

Run: `uv run --extra dev pytest -q tests/test_mutations.py tests/test_entrypoint.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zenmoney_mcp/mutations.py src/zenmoney_mcp/server.py \
  tests/test_mutations.py tests/test_entrypoint.py
git commit -m "feat: prepare monthly recurring payments"
```

### Task 3: Partial month, trimmed mean и expense patterns

**Files:**
- Modify: `src/zenmoney_mcp/planning.py`
- Modify: `src/zenmoney_mcp/server.py`
- Test: `tests/test_planning.py`
- Test: `tests/test_entrypoint.py`

**Interfaces:**
- Consumes: `completed_periods`, `current_period`, `_cash_flow_for_dates`, `user_currency`.
- Produces: `get_spending_baseline(..., include_current_partial_month=False)` с `monthly_series`, `trimmed_mean`, `expense_patterns` и `pattern_summary`.

- [ ] **Step 1: Написать падающий test partial month**

На `as_of=date(2026, 8, 28)` проверить последний row:

```python
assert result["monthly_series"][-1] == {
    "label": "2026-08", "month": "2026-08",
    "start": "2026-08-01", "end": "2026-08-28",
    "complete": False, "days_elapsed": 28, "days_total": 31,
    "outcome": 500,
}
assert result["median"] == completed_only_median
```

- [ ] **Step 2: Проверить RED partial month**

Run: `uv run --extra dev pytest -q tests/test_planning.py -k partial_month`

Expected: FAIL — argument и `monthly_series` отсутствуют.

- [ ] **Step 3: Реализовать partial month и trimmed mean**

Сформировать полные rows с period metadata; optional current row добавить после
статистик. Для trimmed mean удалить `floor(n*0.10)` значений с каждого края и
посчитать `statistics.fmean` оставшихся.

- [ ] **Step 4: Написать и проверить RED seasonality**

Добавить merchant/payee операции с одним событием и стабильными интервалами
30/90/180/365 дней плюс нерегулярную группу. Проверить шесть точных class names,
20% amount-stability guard, summary и limit metadata.

Run: `uv run --extra dev pytest -q tests/test_planning.py -k "expense_patterns or trimmed_mean"`

Expected: FAIL — pattern fields отсутствуют.

- [ ] **Step 5: Реализовать минимальный seasonality detector**

Одним bounded query выбрать one-sided operating outcomes полных периодов,
привести валюты, сгруппировать по normalized merchant/payee + category и
применить диапазоны из spec. Отсортировать по total amount, вернуть первые 100 и
явный `patterns_truncated`.

- [ ] **Step 6: Добавить schema/dispatch и проверить GREEN**

Run: `uv run --extra dev pytest -q tests/test_planning.py tests/test_entrypoint.py`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/zenmoney_mcp/planning.py src/zenmoney_mcp/server.py \
  tests/test_planning.py tests/test_entrypoint.py
git commit -m "feat: analyze budget seasonality"
```

### Task 4: Документация, полный verification и доставка

**Files:**
- Modify: `README.md`
- Modify: `README.ru.md`
- Modify: `docs/planning-semantics.md`

**Interfaces:**
- Produces: пользовательский контракт Phase 3 и production evidence для следующего релиза.

- [ ] **Step 1: Обновить документацию**

Документировать предпочтительное `prepare_changes`, совместимый alias,
single-request boundary, monthly high-level payload, partial/statistics/pattern
semantics и ограничения эвристик.

- [ ] **Step 2: Выполнить verification**

Run: `uv run --python 3.11 --extra dev pytest -q`

Run: `uv run python -m compileall -q src tests`

Run: `git diff --check`

Run: `uv build && python -m zipfile -l dist/*.whl`

Expected: полный test suite PASS; build/compile/diff PASS.

- [ ] **Step 3: Commit docs**

```bash
git add README.md README.ru.md docs/planning-semantics.md
git commit -m "docs: document phase three workflows"
```

- [ ] **Step 4: Review, PR, release, production**

Проверить branch/remote/refspec, push в `origin`, открыть PR, дождаться всех CI,
исправить findings, merge. Дождаться semantic release и immutable GHCR image,
проверить tag/digest/OCI revision, развернуть через production procedure и
выполнить health, mounts, logs, `list_tools`, `get_sync_status` и read-only
`get_spending_baseline` smoke без `force_sync`.
