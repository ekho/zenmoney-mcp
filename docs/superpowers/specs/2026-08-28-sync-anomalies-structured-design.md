# Phase 4: sync ergonomics, anomaly signals and structured MCP results

## Goal

Закрыть P2-часть Change Request: единый UTC-контракт синхронизации,
опциональное ограниченное ожидание `force_sync`, отдельные типы аномалий и
native structured MCP payloads без поломки старых клиентов.

## Constraints

- Существующий асинхронный `force_sync` остаётся поведением по умолчанию.
- Sync worker остаётся единственным процессом с ZenMoney token и правом записи
  финансового cache.
- Никаких новых зависимостей, контейнеров, secrets, env-переменных или volumes.
- Text JSON остаётся совместимым fallback для MCP-клиентов, которые ещё не
  читают `structuredContent`.
- Аналитика остаётся read-only и bounded; финансовые данные не попадают в logs.

## 1. UTC timestamps and bounded sync wait

Файл control state продолжает хранить целые Unix timestamps. Это сохраняет
текущий lock/validation/storage contract и не требует миграции разделяемого
volume. На публичной границе значения преобразуются в RFC3339 UTC с суффиксом
`Z`:

- `requested_at`;
- `started_at`;
- `finished_at`;
- `last_sync_time`;
- `data_quality.last_sync` в planning responses.

`null` остаётся `null`. `last_server_timestamp` остаётся числовым ZenMoney
cursor: это не время выполнения sync и менять его семантику нельзя.

`force_sync` получает `wait_until_complete: boolean = false`. При `false`
ответ остаётся немедленным и single-flight. При `true` сервер ждёт terminal
state того же `request_id`, опрашивая существующий control file. Timeout
фиксирован на 60 секунд и не отменяет продолжающийся sync:

```json
{
  "status": "completed|failed|timeout",
  "state": "completed|failed|pending|running",
  "request_id": "...",
  "mode": "incremental|full",
  "requested_at": "2026-08-28T10:51:35Z",
  "started_at": "2026-08-28T10:51:36Z",
  "finished_at": "2026-08-28T10:51:40Z",
  "failure_code": null,
  "wait_timed_out": false
}
```

Если control state становится невалидным или request ID заменён, ответ
fail-closed использует фиксированный failure code и не возвращает содержимое
control file. Default async response сохраняет прежние поля/status, меняется
только формат `requested_at`.

## 2. Anomaly taxonomy

`detect_anomalies` сохраняет `outliers` и `possible_duplicates` как
совместимые aliases и добавляет отдельные bounded collections:

- `exact_duplicates` — совпадают дата, сумма в user currency до цента,
  normalized merchant/payee, category и outcome account;
- `near_duplicates` — совпадают normalized merchant/payee и category, дата
  отличается не более чем на два дня, сумма отличается не более чем на 5%;
- `same_merchant_amount_close_timestamp` — один merchant/payee, та же сумма до
  цента и разница не более одного дня, но пара не является exact duplicate;
- `periodic_recurrences` — одинаковые amount/category с устойчивой календарной
  периодичностью;
- `unusually_large_one_off` — только положительный z-score выше threshold и
  только если операция не входит в найденную periodic recurrence.

В cache ZenMoney есть дата операции, но нет более точного transaction time.
Поэтому close timestamp честно работает с day precision и сообщает
`timestamp_precision: "day"`.

Для periodicity выполняется один запрос от начала выбранного периода либо за
400 дней до его конца (что раньше). Группы строятся по category и округлённой
до цента сумме в user currency, даты дедуплицируются. Используются уже принятые
диапазоны: monthly 25–35 дней (минимум 3 события), quarterly 80–100,
semiannual 170–195, annual 350–380 (минимум 2 события). Periodic groups
возвращаются как контекст, но их операции исключаются из
`unusually_large_one_off`.

Каждая новая collection возвращает первые 15 результатов; summary содержит
полные counts и общий `results_truncated`. Старые `outliers` и
`possible_duplicates` остаются bounded до 15.

## 3. Native structured MCP results

Установленный MCP SDK 2.x поддерживает `Tool.outputSchema` и
`CallToolResult.structuredContent`. Все ZenMoney tools возвращают JSON object,
поэтому discovery получает общий честный output schema:

```json
{"type": "object"}
```

На protocol boundary существующий JSON `TextContent` один раз декодируется в
тот же object и помещается в `structuredContent`. TextContent сохраняется как
backward-compatible representation. Внутренний `call_tool` contract не
меняется, поэтому существующие тесты и локальные callers не требуют массовой
переделки.

## Safety and verification

TDD покрывает UTC formatting, async compatibility, terminal/timeout/invalid
wait states, каждый anomaly class, periodic-outlier suppression, outputSchema
discovery и равенство structured/text payload. Затем выполняются полный Python
3.11 suite, compileall, diff check, wheel/sdist build, PR CI, semantic release и
production smoke без `force_sync`.
