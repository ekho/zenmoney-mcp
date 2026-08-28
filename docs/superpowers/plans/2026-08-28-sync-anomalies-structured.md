# Phase 4 Sync, Anomaly and Structured Results Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development to execute this plan task by task.

**Goal:** Выпустить UTC sync contract, bounded synchronous wait, расширенную
anomaly taxonomy и native structured MCP responses.

**Architecture:** Сохраняем epoch timestamps внутри существующего file control
channel и преобразуем только публичную границу. Расширяем текущий
`detect_anomalies` одним historical query и совместимыми aliases. На MCP
boundary добавляем `structuredContent`, переиспользуя существующий JSON text.

**Tech Stack:** Python 3.11+, stdlib `asyncio`/`datetime`/`statistics`, SQLite,
MCP SDK 2.x, pytest.

**Spec:** `docs/superpowers/specs/2026-08-28-sync-anomalies-structured-design.md`

## Global constraints

- Не добавлять зависимости, secrets, env-переменные, containers или volumes.
- Не менять внутренний on-disk sync state schema.
- Async `force_sync` остаётся default; timeout не отменяет sync.
- Legacy anomaly keys и JSON TextContent сохраняются.
- Все новые списки и ожидания bounded.

---

### Task 1: UTC sync timestamps и bounded wait

**Files:**
- Modify: `src/zenmoney_mcp/sync_control.py`
- Modify: `src/zenmoney_mcp/server.py`
- Modify: `src/zenmoney_mcp/analytics.py`
- Modify: `src/zenmoney_mcp/planning.py`
- Test: `tests/test_sync_control.py`
- Test: `tests/test_remote_http.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_planning.py`

**Interfaces:**
- Produces: `format_sync_timestamp(int) -> str`.
- Produces: `force_sync(force_full=false, wait_until_complete=false)`.
- Preserves: integer timestamps in `sync-state.json`, single-flight worker.

- [ ] **Step 1: Написать и проверить RED для UTC**

Проверить epoch zero и ненулевой timestamp как `...Z`, затем потребовать строки
или `null` для `requested_at`, `started_at`, `finished_at`, `last_sync_time` и
planning `data_quality.last_sync`.

Run:

```bash
uv run --extra dev pytest -q tests/test_sync_control.py tests/test_remote_http.py \
  tests/test_tools.py tests/test_planning.py -k "sync_timestamp or sync_status_utc"
```

- [ ] **Step 2: Реализовать UTC boundary**

Добавить один stdlib formatter в `sync_control.py`. Использовать его в
`_public_sync_state`, default `force_sync` response,
`get_sync_status_resource` и planning data quality. Invalid metadata сохраняет
текущий fail-closed `null/unknown` contract.

- [ ] **Step 3: Написать RED для wait contract**

Проверить:

- отсутствие флага сохраняет немедленный `accepted`;
- `wait_until_complete=true` возвращает completed/failed terminal state того же
  request ID;
- уже running request остаётся single-flight;
- bounded timeout возвращает `status=timeout`, `wait_timed_out=true` и не
  отменяет pending/running request;
- invalid/replaced state не раскрывается и возвращает fixed failure code;
- unknown fields и не-boolean flags дают `INVALID_PARAMS`.

- [ ] **Step 4: Реализовать минимальный async wait**

Сделать remote control dispatch async. Poll validated control state с
`asyncio.sleep`, constants `60.0s`/`0.25s`; terminal response включает полный
public state. Timeout возвращает текущий state и не пишет control file.

- [ ] **Step 5: Проверить GREEN и commit**

Run:

```bash
uv run --extra dev pytest -q tests/test_sync_control.py tests/test_remote_http.py \
  tests/test_tools.py tests/test_planning.py
uv run --python 3.11 --extra dev pytest -q
git diff --check
```

Commit: `feat: improve sync control ergonomics`

### Task 2: Отдельные anomaly signals

**Files:**
- Modify: `src/zenmoney_mcp/analytics.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Produces: `exact_duplicates`, `near_duplicates`,
  `same_merchant_amount_close_timestamp`, `periodic_recurrences`,
  `unusually_large_one_off`.
- Preserves: `outliers`, `possible_duplicates` and existing inputs.

- [ ] **Step 1: Написать падающие classification tests**

С фиксированными dates/currencies проверить exact, 5%-near, same merchant +
same amount + one-day distance, а также отсутствие overlap по precedence.
Проверить `timestamp_precision="day"`.

- [ ] **Step 2: Написать падающий periodic suppression test**

Три monthly события одной category/amount плюс большой непериодический расход
должны дать recurrence context; periodic transaction не появляется в
`unusually_large_one_off`, one-off появляется. Добавить quarterly/annual edge
и legacy alias assertions.

- [ ] **Step 3: Проверить RED**

Run:

```bash
uv run --extra dev pytest -q tests/test_tools.py -k "anomal"
```

- [ ] **Step 4: Реализовать минимальный classifier**

Одним query загрузить выбранный период и до 400 дней истории. Один раз
нормализовать party, category и converted amount. Pair precedence: exact,
merchant/amount/close-day, near. Periodicity использует unique dates и
25–35/80–100/170–195/350–380 ranges. Outlier — только положительный z-score и
не periodic ID. Вернуть первые 15 каждого типа, полные counts и truncation.

- [ ] **Step 5: Проверить GREEN и commit**

Run:

```bash
uv run --extra dev pytest -q tests/test_tools.py -k "anomal"
uv run --python 3.11 --extra dev pytest -q
git diff --check
```

Commit: `feat: classify transaction anomalies`

### Task 3: Native structured MCP payloads

**Files:**
- Modify: `src/zenmoney_mcp/server.py`
- Test: `tests/test_entrypoint.py`
- Test: `tests/test_remote_http.py`

**Interfaces:**
- Produces: `Tool.outputSchema={"type":"object"}` для всех tools.
- Produces: `CallToolResult.structuredContent` равный JSON object из fallback
  `TextContent`.
- Preserves: direct `call_tool() -> list[TextContent]` и текстовый JSON.

- [ ] **Step 1: Написать и проверить RED**

Discovery должен объявлять object output schema у local и remote tools.
Protocol call должен возвращать одновременно native structured object и
совпадающий serialized TextContent для read и mutation/control results.

Run:

```bash
uv run --extra dev pytest -q tests/test_entrypoint.py tests/test_remote_http.py \
  -k "structured or output_schema"
```

- [ ] **Step 2: Реализовать protocol-boundary adapter**

Добавить object output schema при финальном построении descriptors. В
`create_server` строго декодировать единственный JSON TextContent и передавать
его как `structuredContent`; remote parse/type failure проходит существующий
sanitized internal-error path.

- [ ] **Step 3: Проверить GREEN и commit**

Run:

```bash
uv run --extra dev pytest -q tests/test_entrypoint.py tests/test_remote_http.py
uv run --python 3.11 --extra dev pytest -q
git diff --check
```

Commit: `feat: return structured MCP results`

### Task 4: Документация, verification и доставка

**Files:**
- Modify: `README.md`
- Modify: `README.ru.md`
- Modify: `deploy/remote-mcp/README.md`
- Modify: `docs/planning-semantics.md`

- [ ] **Step 1: Обновить contract docs**

Описать UTC timestamps, fixed 60-second wait/timeout semantics, anomaly
taxonomy/tolerances/day precision/history bound/aliases и native structured
payload с TextContent fallback.

- [ ] **Step 2: Выполнить verification**

```bash
uv run --python 3.11 --extra dev pytest -q
uv run python -m compileall -q src tests
git diff --check
uv build
uv run python -m zipfile -l dist/*.whl
```

- [ ] **Step 3: Commit docs**

Commit: `docs: document phase four contracts`

- [ ] **Step 4: Review, PR, release, production**

Выполнить task reviews и whole-branch review, проверить exact SSH
remote/refspec, push, PR CI, merge, semantic release, PyPI/GHCR. Развернуть
immutable image по production procedure. Post-deploy: source/digest/revision,
health/readiness, mounts/volumes/restarts/log counts, `list_tools`, native
structured `get_sync_status`, read-only `detect_anomalies`; не вызывать
`force_sync` только ради smoke.
