# Phase 2: план реализации финансового планирования, поиска и split

> **Исполнение:** выполнять последовательно в этой задаче через
> `superpowers:executing-plans`; каждый production change начинается с RED.

**Цель:** выпустить и развернуть расширенный debt planner, единый financial
position, cursor search и атомарный transaction split.

**Спецификация:**
`docs/superpowers/specs/2026-08-28-financial-planning-search-split-design.md`

**Ограничения:** Python stdlib и существующий MCP stack; без миграций и новых
зависимостей; `uv run python`/`uv run pytest`; Conventional Commits с
`Co-Authored-By: OpenAI Codex <codex@openai.com>`; production volume не удалять.

## Карта файлов

- `src/zenmoney_mcp/decision/debt.py`: модели и месячная амортизация.
- `src/zenmoney_mcp/planning.py`: financial position поверх общих obligations и flows.
- `src/zenmoney_mcp/financial_correctness.py`: filters, keyset pagination и sorting.
- `src/zenmoney_mcp/entity_changes.py`: нормализация split в замороженные items.
- `src/zenmoney_mcp/mutations.py`: единый атомарный outgoing batch.
- `src/zenmoney_mcp/server.py`: MCP schemas и dispatch.
- `tests/test_decision.py`, `tests/test_planning.py`, `tests/test_tools.py`,
  `tests/test_entity_changes.py`, `tests/test_mutations.py`: unit/regression.
- `tests/test_decision_mcp.py`, `tests/test_planning_mcp.py`,
  `tests/test_entrypoint.py`, `tests/test_remote_http.py`: contracts/integration.
- `README.md`, `README.ru.md`, `docs/planning-semantics.md`: публичный контракт.

---

### Task 1: Расширить `plan_debt_payoff`

- [x] Добавить в `tests/test_decision.py` RED-проверки: credit card grace fields,
  installment schedule, arbitrary liability, all negative account types,
  user-only liability и rollover после payoff.
- [x] Запустить только новые тесты и подтвердить ожидаемые failures старой модели.
- [x] Расширить конфигурацию и симуляцию в `decision/debt.py`, сохранив старые
  `apr_pct + minimum_payment` вызовы.
- [x] Расширить `_DEBT_ACCOUNTS_SCHEMA` в `server.py`; добавить schema/dispatch
  contract tests в `tests/test_decision_mcp.py`.
- [x] Запустить `uv run pytest -q tests/test_decision.py tests/test_decision_mcp.py`.
- [x] Commit: `feat: support real-world liability payoff models`.

### Task 2: Добавить `get_financial_position`

- [x] Добавить RED-тест в `tests/test_planning.py` с loan, ccard, отрицательным
  checking installment и `inBalance=false`; проверить все конечные поля и
  `net_worth = assets - liabilities`.
- [x] Добавить RED contract test инструмента и overrides в
  `tests/test_planning_mcp.py`.
- [x] Реализовать функцию в `planning.py` через `_financial_obligations`,
  `convert` и три `_cash_flow_for_dates`; не вызывать старый `get_net_worth`.
- [x] Добавить tool schema/import/dispatch в `server.py`.
- [x] Запустить `uv run pytest -q tests/test_planning.py tests/test_planning_mcp.py`.
- [x] Commit: `feat: expose unified financial position`.

### Task 3: Сделать transaction search постраничным

- [ ] Добавить RED-тесты в `tests/test_tools.py`: все uncategorized outcomes за
  custom period через несколько страниц, amount DESC, stable ties, массивы
  category/account, categorized/uncategorized и invalid/mismatched cursor.
- [ ] Добавить RED schema/dispatch assertions в `tests/test_entrypoint.py`.
- [ ] Реализовать opaque versioned cursor и keyset conditions в
  `financial_correctness.py`; сохранить одиночные фильтры и limit 1..200.
- [ ] Расширить schema и dispatch в `server.py`; вернуть `next_cursor`,
  `sort_by`, `sort_order`.
- [ ] Запустить `uv run pytest -q tests/test_tools.py tests/test_hardening.py tests/test_entrypoint.py`.
- [ ] Commit: `feat: paginate transaction search`.

### Task 4: Добавить атомарный transaction split

- [ ] Добавить RED normalization tests в `tests/test_entity_changes.py` для
  exact sum, одного remainder, categories, raw metadata, proportional operation
  amounts и отклонения transfer/hold/deleted/stale/invalid parts.
- [ ] Добавить RED executor test в `tests/test_mutations.py`: один `push_changes`
  содержит update исходной и create частей; повторный apply не отправляет batch.
- [ ] Добавить RED schema tests split для entity-specific и mixed tools.
- [ ] Реализовать минимальный split helper в `entity_changes.py`, разворачивающий
  одну клиентскую operation в несколько proposal items с UUID.
- [ ] Убедиться, что `execute_proposal` отправляет все split items одним batch;
  refs не менять до Phase 3.
- [ ] Расширить mutation schemas в `server.py`.
- [ ] Запустить `uv run pytest -q tests/test_entity_changes.py tests/test_mutations.py tests/test_entrypoint.py tests/test_remote_http.py`.
- [ ] Commit: `feat: prepare atomic transaction splits`.

### Task 5: Документация и полный Phase 2 gate

- [ ] Обновить README EN/RU и planning semantics с точными примерами новых
  аргументов и экономической семантикой.
- [ ] Запустить `uv run pytest -q` и `git diff --check` на чистом кандидате.
- [ ] Проверить `tools/list`, четыре новых сценария локального MCP и отсутствие
  финансовых значений в логах/артефактах.
- [ ] Commit: `docs: document phase two financial tools`.
- [ ] Push точного refspec, открыть PR, дождаться CI, review, merge.
- [ ] Выпустить minor release `v0.7.0`; проверить GitHub release, tag и GHCR OCI revision.
- [ ] Развернуть immutable `ghcr.io/ekho/zenmoney-mcp:0.7.0` в production по
  production-deploy skill без `down -v`; проверить health, source SHA, tools и
  read-only smoke.
- [ ] Зафиксировать Phase 2 как завершённую только после live production evidence.
