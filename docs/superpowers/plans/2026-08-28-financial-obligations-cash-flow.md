# Финансовые обязательства и денежный поток: план реализации

> **Для исполнителей:** используйте `superpowers:subagent-driven-development`
> или `superpowers:executing-plans` и выполняйте план по задачам. Для прогресса
> используются флажки (`- [ ]`).

**Цель:** Ввести конечные breaking-контракты `get_cash_flow` и `get_debt_service`, которые корректно разделяют потребление, финансирование, внутренние переводы и денежное обслуживание всех активных обязательств.

**Архитектура:** Общий сборщик обязательств и общий классификатор потоков остаются в `planning.py`; все planning-расчёты используют эти функции вместо отдельных SQL-правил. `server.py` только описывает строгую MCP-схему и передаёт аргументы, а два существующих decision-потребителя адаптируются к новому контракту без расширения payoff planner.

**Технологии:** Python 3.11+, stdlib `sqlite3`/`datetime`/`statistics`, MCP SDK 2.x, pytest, jsonschema из обязательной зависимости MCP.

**Спецификация:** `docs/superpowers/specs/2026-08-28-financial-obligations-cash-flow-design.md`

## Общие ограничения

- Не добавлять зависимости, таблицы, миграции или отдельный domain-модуль.
- Не использовать названия счетов, категории, merchant, payee или комментарии как кредитные эвристики.
- Любой активный отрицательный счёт является обязательством; `inBalance` не исключает его.
- Неизвестные APR, суммы и даты платежей возвращать как `null`, не как `0`.
- `get_cash_flow` и `get_debt_service` возвращают только конечные поля, без compatibility aliases.
- Не расширять типы долгов, поддерживаемые `plan_debt_payoff`; это отдельная P1-фаза.
- Все денежные стороны конвертировать через существующий `money.convert`; отсутствующий курс остаётся явной ошибкой.
- Рабочие команды: `uv run pytest`, `uv run python`; live-тесты и реальные финансовые данные не использовать.
- Каждый commit использует Conventional Commits и trailer `Co-Authored-By: OpenAI Codex <codex@openai.com>`.

## Карта файлов

- `src/zenmoney_mcp/planning.py`: сбор обязательств, классификация потоков и оба конечных read-only контракта.
- `src/zenmoney_mcp/server.py`: исправление schema patcher, schema overrides и dispatch.
- `src/zenmoney_mcp/decision/debt.py`: адаптер нового контракта к прежнему набору `loan`/`debt` payoff planner.
- `src/zenmoney_mcp/decision/scenarios.py`: чтение прежнего debt subset из нового ответа.
- `tests/test_planning.py`: финансовые regression-тесты и внутренние planning-потребители.
- `tests/test_planning_mcp.py`: реальные `tools/list` schemas и MCP dispatch.
- `tests/test_decision.py`, `tests/test_decision_mcp.py`: отсутствие регрессий decision tools.
- `docs/planning-semantics.md`, `README.md`, `README.ru.md`: пользовательская семантика конечных контрактов.

---

### Task 1: Устранить конфликт enum и regex в `get_cash_flow.period`

**Files:**
- Modify: `src/zenmoney_mcp/server.py:242-280`
- Test: `tests/test_planning_mcp.py:41-81`

**Interfaces:**
- Consumes: `server.list_tools() -> list[Tool]` и существующий `_PERIOD_PATTERN`.
- Produces: `harden_tool_schemas()` не добавляет `pattern` свойству с `enum`, но сохраняет regex для legacy period schemas.

- [ ] **Step 1: Написать падающий contract test для всех planning presets**

Добавить импорт и параметризованный тест:

```python
from jsonschema import validate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "period",
    [
        "current_period",
        "last_complete_month",
        "last_30_days",
        "trailing_3_complete_months",
        "trailing_6_complete_months",
        "trailing_12_complete_months",
    ],
)
async def test_cash_flow_schema_accepts_every_documented_period(period):
    tools = {tool.name: tool.input_schema for tool in await server.list_tools()}
    schema = tools["get_cash_flow"]

    validate({"period": period}, schema)
    assert "pattern" not in schema["properties"]["period"]
```

- [ ] **Step 2: Запустить тест и подтвердить воспроизводимый FAIL**

Run: `uv run pytest -q tests/test_planning_mcp.py::test_cash_flow_schema_accepts_every_documented_period`

Expected: шесть ошибок `ValidationError` либо assertion failure из-за старого `pattern`.

- [ ] **Step 3: Исправить общий schema patcher одной проверкой**

В `harden_tool_schemas` заменить безусловную ветку периода:

```python
elif name == "period" and "enum" not in value:
    value["pattern"] = _PERIOD_PATTERN
```

- [ ] **Step 4: Проверить planning и legacy schemas**

Run: `uv run pytest -q tests/test_planning_mcp.py tests/test_entrypoint.py`

Expected: PASS; `get_cash_flow.period` без regex, а legacy `period` по-прежнему имеет `_PERIOD_PATTERN`.

- [ ] **Step 5: Зафиксировать исправление**

```bash
git add src/zenmoney_mcp/server.py tests/test_planning_mcp.py
git commit -m "fix: accept every cash flow period preset" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 2: Добавить единый сборщик финансовых обязательств

**Files:**
- Modify: `src/zenmoney_mcp/planning.py:20-49,374-442`
- Test: `tests/test_planning.py:20-149,453-557`

**Interfaces:**
- Consumes: `user_currency(db) -> CurrencyContext`, `convert(db, amount, instrument_id, currency) -> float`, `parse_iso_date`, `non_negative_number`.
- Produces: `_financial_obligations(db, currency, overrides=None, *, as_of=None) -> list[dict[str, Any]]` с конечной структурой obligation.

- [ ] **Step 1: Написать падающие тесты классификации и `inBalance`**

Импортировать `_financial_obligations` и добавить отрицательные `debt`,
`checking` и неизвестный тип. Проверить точный порядок `title,id`, положительный
`balance`, default classification и включение `in_balance=0`:

```python
def test_financial_obligations_include_every_active_negative_account(planning_db):
    planning_db.connect().executemany(
        "INSERT INTO accounts(id,title,type,instrument,balance,in_balance,savings,archive,user,changed) "
        "VALUES (?,?,?,?,?, ?,0,0,1,1)",
        [
            ("installment", "Installment", "checking", 1, -300, 0),
            ("personal", "Personal", "debt", 1, -200, 1),
            ("odd", "Odd", "cash", 1, -100, 1),
        ],
    )

    obligations = _financial_obligations(
        planning_db, user_currency(planning_db), as_of=date(2026, 8, 23)
    )

    by_id = {item["account_id"]: item for item in obligations}
    assert by_id["loan"]["classification"] == "loan"
    assert by_id["credit"]["classification"] == "credit_card"
    assert by_id["personal"]["classification"] == "personal_debt"
    assert by_id["installment"]["classification"] == "other"
    assert by_id["installment"]["classification_confidence"] == "low"
    assert by_id["installment"]["in_balance"] is False
    assert by_id["installment"]["balance"] == 300
    assert by_id["odd"]["classification"] == "other"
```

Добавить отдельный тест, что zero/positive и archived accounts исключаются.

- [ ] **Step 2: Запустить новые тесты и подтвердить FAIL по отсутствующему helper**

Run: `uv run pytest -q tests/test_planning.py -k financial_obligations`

Expected: import failure для `_financial_obligations`.

- [ ] **Step 3: Реализовать минимальную классификацию отрицательных счетов**

Добавить константы и сигнатуру:

```python
_OBLIGATION_CLASSES = {
    "loan": ("loan", "high"),
    "ccard": ("credit_card", "high"),
    "debt": ("personal_debt", "high"),
}
_VALID_OBLIGATION_CLASSES = {
    "loan", "credit_card", "installment", "personal_debt", "other"
}


def _financial_obligations(
    db: Any,
    currency: CurrencyContext,
    overrides: dict[str, Any] | None = None,
    *,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
```

Запросить все неархивные accounts, конвертировать баланс, оставить только
`balance < 0`, применить `_OBLIGATION_CLASSES.get(type, ("other", "low"))` и
вернуть неизвестные payment/APR как `null`/`unknown`.

- [ ] **Step 4: Написать падающие тесты строгих overrides**

Проверить:

```python
def test_financial_obligation_overrides_are_explicit_and_strict(planning_db):
    obligations = _financial_obligations(
        planning_db,
        user_currency(planning_db),
        {
            "credit": {
                "classification": "installment",
                "minimum_payment": {"amount": 250, "due_date": "2026-09-18"},
                "apr_pct": 19.9,
            }
        },
        as_of=date(2026, 8, 23),
    )
    credit = next(item for item in obligations if item["account_id"] == "credit")
    assert credit["classification"] == "installment"
    assert credit["classification_confidence"] == "high"
    assert credit["minimum_payment"] == {
        "amount": 250,
        "due_date": "2026-09-18",
        "source": "user_override",
        "confidence": "high",
    }
    assert credit["apr_pct"] == {"value": 19.9, "source": "user_override"}
```

Параметризовать ошибки: unknown account, positive account, unknown field,
invalid classification, negative amount, malformed date и более 50 overrides.

- [ ] **Step 5: Реализовать строгую проверку overrides**

Разрешить ровно `classification`, `minimum_payment`, `apr_pct`; для nested
payment разрешить ровно `amount`, `due_date` и требовать `amount`. Использовать
`non_negative_number` и `parse_iso_date`, а ошибки формировать с путём вида
`obligation_overrides.credit.minimum_payment.due_date`.

- [ ] **Step 6: Написать падающий тест ReminderMarker estimate**

Создать planned marker с `outcome_account="cash-rub"`,
`income_account="loan"`, обеими положительными сторонами и датой
`2026-09-18`. Проверить amount по source-side, source `reminder`, confidence
`medium` и ближайшую due date. Добавить более поздний marker, чтобы доказать
выбор ближайшего.

- [ ] **Step 7: Реализовать nearest planned repayment lookup**

Одним запросом выбрать planned reminder markers с `date >= as_of`, обеими
положительными сторонами и destination obligation. Исключить архивный,
`in_balance=0` или сам являющийся обязательством source account. Конвертировать
`outcome` по `outcome_instrument`; override всегда имеет приоритет.

- [ ] **Step 8: Запустить obligation tests**

Run: `uv run pytest -q tests/test_planning.py -k 'financial_obligation or reminder_payment'`

Expected: PASS.

- [ ] **Step 9: Зафиксировать сборщик обязательств**

```bash
git add src/zenmoney_mcp/planning.py tests/test_planning.py
git commit -m "feat: classify financial obligations" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 3: Ввести общий flow classifier и конечный `get_cash_flow`

**Files:**
- Modify: `src/zenmoney_mcp/planning.py:49-289,293-350,641-684`
- Test: `tests/test_planning.py:159-379,673-727`

**Interfaces:**
- Consumes: `_financial_obligations(...)`, `convert(...)`, `resolve_period(...)`.
- Produces: `_cash_flow_for_dates(db, start, end, category_ids=None) -> dict[str, Any]` с новыми flow totals; `get_cash_flow(...)` с конечным публичным контрактом.

- [ ] **Step 1: Переписать существующие cash-flow assertions на final contract**

В `test_cash_flow_excludes_transfers_holds_and_external_accounts` проверять:

```python
assert result["income"] == 1_000
assert result["operating_expenses"] == 200
assert result["operating_net_cash_flow"] == 800
assert result["financing_inflow"] == 0
assert result["debt_service_cash_outflow"] == 0
assert result["net_cash_flow_after_debt_service"] == 800
assert result["savings_rate_before_debt_service_pct"] == 80
assert result["savings_rate_after_debt_service_pct"] == 80
assert result["flow_components"]["operating_expense"] == {
    "amount": 200,
    "count": 1,
}
assert "outcome" not in result
assert "net_cash_flow" not in result
assert "savings_rate_pct" not in result
```

Обновить zero-income, FX, period и 50,000-row тесты на новые поля.

- [ ] **Step 2: Добавить три падающих acceptance tests потоков**

Добавить:

```python
def test_cash_flow_separates_debt_service_from_operating_expense(planning_db):
    add_transaction(
        planning_db, "payment", "2026-07-10", income=100_000, outcome=100_000,
        income_account="loan", outcome_account="cash-rub",
    )
    result = get_cash_flow(
        planning_db, start_date="2026-07-01", end_date="2026-07-31"
    )
    assert result["operating_expenses"] == 0
    assert result["debt_service_cash_outflow"] == 100_000
    assert result["net_cash_flow_after_debt_service"] == -100_000


def test_cash_flow_does_not_count_borrowing_as_income(planning_db):
    add_transaction(
        planning_db, "borrowing", "2026-07-10", income=300_000, outcome=300_000,
        income_account="cash-rub", outcome_account="loan",
    )
    result = get_cash_flow(
        planning_db, start_date="2026-07-01", end_date="2026-07-31"
    )
    assert result["income"] == 0
    assert result["financing_inflow"] == 300_000
    assert result["net_cash_flow_after_debt_service"] == 300_000


def test_liability_funded_spending_has_equal_expense_and_financing(planning_db):
    add_transaction(
        planning_db, "card-purchase", "2026-07-10", outcome=30_000,
        outcome_account="credit", tag="food",
    )
    result = get_cash_flow(
        planning_db, start_date="2026-07-01", end_date="2026-07-31"
    )
    assert result["operating_expenses"] == 30_000
    assert result["financing_inflow"] == 30_000
    assert result["net_cash_flow_after_debt_service"] == 0
```

- [ ] **Step 3: Запустить cash-flow tests и подтвердить FAIL старого SQL**

Run: `uv run pytest -q tests/test_planning.py -k 'cash_flow or liability_funded'`

Expected: старые keys/values и отсутствие transfer classification.

- [ ] **Step 4: Переписать `_cash_flow_for_dates` на один полный range scan**

Убрать SQL-фильтр, исключающий transfers. Выбрать для обеих сторон account ID,
type, balance, `in_balance`, savings и archive. Построить set obligation IDs из
`_financial_obligations` и семь buckets:

```python
names = (
    "income", "operating_expense", "internal_transfer", "financing_inflow",
    "debt_service_outflow", "asset_transfer", "unknown",
)
components = {name: {"amount": 0.0, "count": 0} for name in names}
```

В одном loop реализовать таблицу из spec. Для liability-funded outcome добавить
сумму и count одновременно в `operating_expense` и `financing_inflow`.
Debt-service считать по outcome side, borrowing - по income side. Transfers с
savings account или account type `deposit` относить к `asset_transfer`.

- [ ] **Step 5: Вернуть bounded uncertain transactions и warning**

Для structurally unknown row добавить не более 50 записей:

```python
{
    "transaction_id": row["id"],
    "classification": "unknown",
    "classification_reason": "account_relationship_is_not_classifiable",
    "confidence": "low",
}
```

Если unknown count положителен, добавить
`"unknown_transaction_flows_excluded"` в `data_quality.warnings`.

- [ ] **Step 6: Сформировать final `get_cash_flow` formulas**

Использовать новые private totals и вернуть только поля spec. Проценты равны
`None` при `income == 0`; все суммы округлять до двух знаков после агрегации.

- [ ] **Step 7: Адаптировать внутренние planning consumers без изменения их API**

Заменить private-key reads:

```python
# spending baseline and emergency fund
totals["operating_expenses"]

# compare_periods keeps its existing public "outcome" label
_delta(a["operating_expenses"], b["operating_expenses"])

# financial snapshot keeps its existing nested contract for Phase 3 consumers
{
    "income": last["income"],
    "outcome": last["operating_expenses"],
    "net_cash_flow": last["operating_net_cash_flow"],
}
```

`_average_cash_flow` также сохраняет nested `income/outcome/net_cash_flow`, где
`outcome` равен operating expenses, а net не вычитает debt service повторно в
существующем integrated planner.

- [ ] **Step 8: Запустить весь planning module**

Run: `uv run pytest -q tests/test_planning.py`

Expected: PASS, включая 50,000-row scan.

- [ ] **Step 9: Зафиксировать flow classifier**

```bash
git add src/zenmoney_mcp/planning.py tests/test_planning.py
git commit -m "feat: classify household cash flows" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 4: Перевести `get_debt_service` и прямых потребителей на final contract

**Files:**
- Modify: `src/zenmoney_mcp/planning.py:374-442`
- Modify: `src/zenmoney_mcp/server.py:410-412,432-468,1885-1887`
- Modify: `src/zenmoney_mcp/decision/debt.py:82-129`
- Modify: `src/zenmoney_mcp/decision/scenarios.py:136-153`
- Test: `tests/test_planning.py:453-557`
- Test: `tests/test_planning_mcp.py:41-93`
- Test: `tests/test_decision.py:183-365`
- Test: `tests/test_decision_mcp.py:69-125`

**Interfaces:**
- Consumes: `_financial_obligations(...)` и `_cash_flow_for_dates(...)` из Tasks 2-3.
- Produces: `get_debt_service(db, obligation_overrides=None, *, as_of=None) -> dict[str, Any]` и строгую MCP-schema `obligation_overrides`.

- [ ] **Step 1: Переписать debt-service tests на final contract**

Основной acceptance test должен проверять `total_liabilities == 7_000` для
существующих `loan` и negative `ccard`, obligations IDs, новый monthly key и
trailing object:

```python
assert result["total_liabilities"] == 7_000
assert {item["account_id"] for item in result["obligations"]} == {"credit", "loan"}
assert result["last_complete_month"] == {
    "operating_income": 1_000,
    "debt_service_cash_outflow": 500,
    "debt_service_ratio_pct": 50,
}
assert result["trailing_3_complete_months"] == {
    "average_debt_service_cash_outflow": 166.67,
}
assert "current_debt_balance" not in result
assert "accounts" not in result
assert "trailing_3_month_average_payment" not in result
```

Обновить zero-income, positive account и source-side FX tests на новые keys.

- [ ] **Step 2: Добавить MCP schema и dispatch tests для overrides**

Проверить `maxProperties == 50`, enum classification, nested
`minimum_payment.required == ["amount"]`, `additionalProperties is False` на
каждом уровне и передачу override через `server.call_tool("get_debt_service", ...)`.

- [ ] **Step 3: Запустить targeted tests и подтвердить FAIL старого контракта**

Run: `uv run pytest -q tests/test_planning.py -k debt_service tests/test_planning_mcp.py`

Expected: старые result fields и пустая schema аргументов.

- [ ] **Step 4: Реализовать final `get_debt_service` через общие helpers**

Для трёх completed periods вызвать `_cash_flow_for_dates`; latest income взять
из `income`, платеж - из `debt_service_cash_outflow`. `total_liabilities` равен
сумме obligation balances. Удалить отдельный `_debt_payments_for_dates`, чтобы
правила не могли разойтись.

- [ ] **Step 5: Добавить строгую MCP-schema и передать overrides в dispatch**

Schema account override:

```python
{
    "type": "object",
    "minProperties": 1,
    "additionalProperties": False,
    "properties": {
        "classification": {"type": "string", "enum": [
            "loan", "credit_card", "installment", "personal_debt", "other"
        ]},
        "minimum_payment": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "minimum": 0},
                "due_date": {"type": "string", "pattern": _DATE_PATTERN},
            },
            "required": ["amount"],
            "additionalProperties": False,
        },
        "apr_pct": {"type": "number", "minimum": 0},
    },
}
```

В dispatch вызвать `get_debt_service(db, arguments.get("obligation_overrides"))`.

- [ ] **Step 6: Адаптировать payoff planner без расширения P1-функциональности**

В `decision/debt.py` преобразовать только прежние `loan`/`debt` obligations в
старую внутреннюю форму:

```python
active = [
    {
        "id": item["account_id"],
        "title": item["title"],
        "debt_balance": item["balance"],
    }
    for item in facts["obligations"]
    if item["source_account_type"] in {"loan", "debt"}
]
```

Так negative `ccard` уже виден в high-level debt service, но не начинает
требовать APR в старом planner раньше отдельной P1-задачи.

- [ ] **Step 7: Адаптировать scenario debt balance к прежней границе**

В `decision/scenarios.py` заменить `current_debt_balance` на сумму balances
obligations с `source_account_type in {"loan", "debt"}`. Остальную scenario
математику не менять.

- [ ] **Step 8: Запустить planning, decision и MCP suites**

Run: `uv run pytest -q tests/test_planning.py tests/test_planning_mcp.py tests/test_decision.py tests/test_decision_mcp.py`

Expected: PASS.

- [ ] **Step 9: Зафиксировать final debt contract**

```bash
git add src/zenmoney_mcp/planning.py src/zenmoney_mcp/server.py \
  src/zenmoney_mcp/decision/debt.py src/zenmoney_mcp/decision/scenarios.py \
  tests/test_planning.py tests/test_planning_mcp.py \
  tests/test_decision.py tests/test_decision_mcp.py
git commit -m "feat: report universal debt service" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 5: Обновить документацию и выполнить полный release gate

**Files:**
- Modify: `docs/planning-semantics.md`
- Modify: `README.md`
- Modify: `README.ru.md`
- Verify: all modified source and test files

**Interfaces:**
- Consumes: final contracts from Tasks 1-4.
- Produces: current user-facing semantics and complete verification evidence.

- [ ] **Step 1: Обновить planning semantics**

Добавить разделы `Financial obligations` и `Cash-flow components` с таблицей
классификации, формулами из spec, правилом `inBalance`, nullable unknown terms и
явным ограничением для unlinked single-sided inflows. Существующее описание
payoff planner оставить scoped к `loan`/`debt` до P1.

- [ ] **Step 2: Обновить краткие README-описания на двух языках**

В разделах planning guarantees явно указать:

```text
get_cash_flow separates operating expenses, financing inflows, and cash debt service;
get_debt_service includes every active negative-balance account regardless of inBalance;
unknown APR and payment terms remain null unless supplied explicitly.
```

Русский README должен содержать тот же смысл без ссылки на английский текст.

- [ ] **Step 3: Проверить документацию и зафиксировать**

Run: `git diff --check`

Expected: no output.

```bash
git add docs/planning-semantics.md README.md README.ru.md
git commit -m "docs: explain obligation cash flow semantics" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

- [ ] **Step 4: Запустить полный non-live suite**

Run: `uv run pytest -q`

Expected: все tests PASS, только заранее существующие live skips.

- [ ] **Step 5: Проверить импорт и bytecode compilation**

Run: `uv run python -m compileall -q src tests`

Expected: exit code 0, no output.

- [ ] **Step 6: Проверить реальную MCP-схему конечных инструментов**

Run:

```bash
uv run python - <<'PY'
import asyncio
from zenmoney_mcp import server

async def main():
    tools = {tool.name: tool.input_schema for tool in await server.list_tools()}
    cash = tools["get_cash_flow"]["properties"]["period"]
    debt = tools["get_debt_service"]["properties"]["obligation_overrides"]
    assert "pattern" not in cash
    assert len(cash["enum"]) == 6
    assert debt["maxProperties"] == 50

asyncio.run(main())
PY
```

Expected: exit code 0, no output.

- [ ] **Step 7: Проверить чистоту и точный diff**

Run: `git status --short --branch && git diff --check && git log --oneline origin/main..HEAD`

Expected: clean `codex/financial-obligations-cash-flow`; только согласованные docs, source и tests commits. Push, PR, release и production deploy не выполняются этой задачей.
