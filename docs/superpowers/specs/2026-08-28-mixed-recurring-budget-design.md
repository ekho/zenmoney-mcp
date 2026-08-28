# Фаза 3: смешанные изменения, плановые платежи и бюджетная аналитика

## Цель

Добавить три P1-возможности из Change Request без новой подсистемы и без новых
зависимостей:

- единый публичный `prepare_changes` для разных ZenMoney-сущностей;
- high-level `prepare_recurring_payment` для обычного ежемесячного расхода;
- частичный текущий месяц, устойчивые статистики и классификация периодичности
  в `get_spending_baseline`.

Существующие proposal store, нормализация сущностей, `/v8/diff/`, двухшаговое
подтверждение и бюджетные периоды остаются единственными реализациями этих
контрактов.

## Смешанный proposal

`prepare_changes` получает тот же строгий `operations[]`, который уже принимает
`prepare_mixed_changes`. Старое имя остаётся совместимым alias, но документация
использует новое имя.

Ссылки `{"ref": "..."}` разрешаются в UUID при подготовке proposal. Поэтому при
применении все rebuilt-объекты можно отправить одним mixed `/v8/diff/` запросом,
включая связанные create для Tag, Transaction, Reminder и ReminderMarker.
Предварительный sync и проверка всех `changed` происходят до единственной записи.

Атомарность здесь означает один upstream write request и один локальный proposal.
Внешний API остаётся границей неопределённости: сетевой сбой после отправки не
повторяется автоматически и переводит весь proposal в `needs_review` с
`write_result_unknown`. Повторный `apply_changes` terminal proposal не пишет
повторно.

## Ежемесячный плановый платёж

`prepare_recurring_payment` принимает:

```json
{
  "name": "Т-Банк кредитка",
  "amount": 28060,
  "account_id": "...",
  "category_id": "...",
  "frequency": "monthly",
  "day_of_month": 18,
  "start_date": "2026-09-18",
  "end_date": null,
  "notify": true
}
```

Фаза 3 намеренно поддерживает только `monthly`: это единственный запрошенный
пользовательский сценарий. День `start_date` обязан совпадать с
`day_of_month`; сервер не сдвигает дату молча. `end_date`, если задана, не может
быть раньше `start_date`.

Сервер находит активный Account, его owner и instrument, проверяет Tag того же
owner и компилирует ввод в один mixed proposal:

- Reminder с `interval=month`, `step=1`, `points=[0]`, `payee=name`;
- первый ReminderMarker на `start_date`, связанный ref с новым Reminder.

Обе сущности получают односторонний расход (`income=0`, `outcome=amount`), один
Account/Instrument, один Tag и одинаковый `notify`. Далее используется обычный
`prepare_changes`; отдельного write path нет.

## Бюджетная и сезонная аналитика

`get_spending_baseline` получает boolean
`include_current_partial_month=false`. `months` по-прежнему означает число
полных бюджетных месяцев. При включённом флаге в конец `monthly_series`
добавляется текущий период до `as_of`:

```json
{
  "month": "2026-08",
  "complete": false,
  "days_elapsed": 28,
  "days_total": 31,
  "outcome": 0
}
```

Частичный месяц не входит в mean, median, квартиль, min/max, trimmed mean и
распознавание периодичности. Старое поле `monthly` временно остаётся alias того
же ряда для совместимости.

`trimmed_mean` использует только stdlib: значения сортируются, затем от каждого
края удаляется `floor(n * 0.10)` наблюдений. Если это ноль, результат совпадает
с обычным mean. Ответ явно сообщает метод.

`expense_patterns` анализирует полные выбранные месяцы. Операционные расходы
группируются по нормализованному merchant/payee и категории, суммы приводятся в
валюту пользователя. Классификация детерминирована:

- `recurring_monthly`: минимум 3 события, все интервалы 25–35 дней;
- `likely_quarterly`: минимум 2 события, все интервалы 80–100 дней;
- `likely_semiannual`: минимум 2 события, все интервалы 170–195 дней;
- `likely_annual`: минимум 2 события, все интервалы 350–380 дней;
- `one_off`: одно наблюдение в выбранном окне, confidence `low`;
- `unknown`: остальные группы.

Периодические классы требуют разброс суммы не более 20% от среднего. Это
эвристика, а не утверждение о будущем платеже. Ответ содержит метод, сводку по
классам, полное число групп и максимум 100 самых крупных групп; truncation
обозначается явно.

## Ошибки и безопасность

Все новые MCP-схемы закрыты через `additionalProperties=false`. Неверная дата,
частота, несовпадающий день, отсутствующий/архивный Account, чужой Tag или
невалидная сумма дают существующий безопасный `invalid_changes` без деталей
upstream и без записи. `prepare_recurring_payment` недеструктивен; только
`apply_changes` остаётся destructive/open-world.

## Проверка

TDD покрывает одну mixed запись с cross-ref, terminal-state идемпотентность,
компиляцию Reminder/ReminderMarker, строгую schema и dispatch, custom budget
month, исключение partial из статистик, trimmed mean и все шесть классов
periodicity. После полного набора выполняются wheel/sdist smoke, PR CI, merge,
семантический релиз и production smoke через `list_tools` и read-only analytics.
