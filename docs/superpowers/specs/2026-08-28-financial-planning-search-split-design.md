# Финансовое планирование, поиск и split: дизайн Phase 2

## Цель

Закрыть Phase 2 Change Request без новых зависимостей и параллельной модели
данных: расширить существующий planner на все активные обязательства, дать единый
экономический срез позиции, сделать поиск транзакций полноценно постраничным и
добавить безопасный split через существующий proposal/apply lifecycle.

## Расширенная модель обязательств

`plan_debt_payoff` продолжает принимать конфигурацию `debt_accounts`,
индексированную идентификатором обязательства. В расчёт попадают все активные
отрицательные счета из `get_debt_service`, независимо от `account.type` и
`inBalance`. Дополнительно разрешена пользовательская запись, которой нет в
снимке, если для неё явно заданы `liability_type="arbitrary"`, `balance` и
`minimum_payment`.

Для записи поддерживаются четыре модели:

- `fixed_loan`: APR и `fixed_payment`; прежний `minimum_payment` остаётся
  допустимым именем того же платежа для обратной совместимости;
- `credit_card`: APR, минимальный платёж, отдельно `statement_balance`,
  `grace_period_payment` и `grace_period_due_date`;
- `installment`: произвольный будущий `payment_schedule` из дат и сумм, APR по
  умолчанию равен нулю;
- `arbitrary`: баланс и минимальный платёж, APR по умолчанию равен нулю.

Если `liability_type` не указан, он выводится только из уже подтверждённой
классификации obligation: loan, credit card, installment или arbitrary. Названия
счетов и транзакций не используются как эвристика. Пользовательский `balance`
может явно заменить баланс снимка для сценарного расчёта.

Каждый месяц planner сначала начисляет проценты, затем вносит обязательные
платежи. Для installment это сумма элементов schedule в соответствующем
календарном месяце; для credit card grace payment добавляется в месяц due date и
не дублирует меньший minimum payment. В avalanche, snowball и custom общий
плановый бюджет месяца сохраняется после досрочного погашения: неиспользованная
часть целиком переходит следующему обязательству. `minimum_only` сохраняет
буквальную семантику и не распределяет сверх обязательных платежей.

## `get_financial_position`

Новый read-only инструмент использует `_financial_obligations` и общий cash-flow
classifier. Он возвращает поля Change Request:

```text
liquid_assets, restricted_assets, total_assets,
loans, credit_cards, installments, personal_debts, total_liabilities,
net_worth,
operating_monthly_income, operating_monthly_expenses,
monthly_debt_service, free_cash_flow_after_debt_service
```

Все суммы выражены в валюте пользователя. Активы — положительные части всех
неархивных счетов: cash/checking/emoney/ccard и доступные savings относятся к
liquid, остальные положительные остатки — к restricted. Обязательства —
положительные модули всех отрицательных неархивных счетов независимо от
`inBalance`; класс `other` включается в `personal_debts`, чтобы итог не терял
реальный долг. `net_worth = total_assets - total_liabilities`.

Месячные flow-поля — средние за три полных пользовательских бюджетных месяца.
Финансирование не считается операционным доходом, а debt service вычитается из
free cash flow. Ответ сообщает эту basis явно.

## Постраничный поиск транзакций

`search_transactions` сохраняет прежние одиночные фильтры и добавляет
`category_ids`, `account_ids`, `category_state`, `sort_by`, `sort_order` и
opaque `cursor`. Массив и одиночное значение одного фильтра объединяются.

Сортировка стабильна:

- `date`: date, changed, id;
- `amount`: сумма в валюте пользователя, затем date, changed, id.

Направление всех tie-breakers совпадает с `sort_order`. Cursor содержит версию,
режим сортировки и последний sort key; он проверяется и не принимается при другом
режиме сортировки. Запрос получает `limit + 1`, возвращает не более 200 строк и
`next_cursor`. `total_matching` считает все строки фильтра без учёта cursor.
Uncategorized означает `tag IS NULL` или пустой JSON-массив.

## Атомарный split

`prepare_transaction_changes` и mixed schema принимают операцию:

```json
{
  "operation": "split",
  "transaction_id": "...",
  "parts": [
    {"amount": 73000, "category_id": "..."},
    {"amount": "remainder", "category_id": "..."}
  ]
}
```

Split разрешён только для полной, неудалённой, неподтверждаемой hold-операции с
одной денежной стороной (income либо outcome). Нужно минимум две положительные
части; `remainder` допускается ровно один, а итог обязан точно совпасть с исходной
суммой.

Нормализатор разворачивает split внутри одного proposal в update исходной
транзакции и create остальных частей. Клиент не эмулирует эти действия. Все
объекты отправляются одним mixed `/v8/diff/` запросом, поэтому сервер не может
применить половину split. Первая часть сохраняет исходный ID, остальные получают
UUID. Каждая часть копирует исходный raw object, включая bank IDs, `created`,
original payee, MCC, reminder marker и неизвестные sync-поля; меняются только ID,
сумма, соответствующая operation-currency сумма, категория и `changed`.
Operation-currency сумма делится в той же пропорции с точным остатком в последней
части.

Proposal замораживает `expected_changed`; внешний edit до apply отклоняет весь
batch. Существующая terminal-state идемпотентность `apply_changes` гарантирует,
что повторный apply того же proposal не отправляет split второй раз. Проверочный
full sync должен увидеть все части с ожидаемыми суммами и категориями.

## Ошибки и ограничения

- Неверные money/date/type/cursor поля дают `InputValidationError` либо закрытый
  mutation failure code без отражения чувствительных данных.
- Неполный raw snapshot не позволяет подготовить split.
- Split transfer-транзакции отклоняется: распределение двух валютных сторон без
  отдельного контракта неоднозначно.
- Вся денежная арифметика planner и split использует `Decimal`; публичные суммы
  округляются существующими money helpers.
- Новые зависимости, таблицы и отдельный service layer не добавляются.

## Проверка

Каждое поведение вводится через падающий тест. Помимо unit/contract tests,
проверяются полный `uv run pytest -q`, remote MCP smoke без финансовых значений и
production deploy с сохранением volume и проверкой OCI revision.
