# ZenMoney MCP Server

[![CI](https://github.com/ekho/zenmoney-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ekho/zenmoney-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/zenmoney-mcp-server.svg)](https://pypi.org/project/zenmoney-mcp-server/)
[![Python](https://img.shields.io/pypi/pyversions/zenmoney-mcp-server.svg)](https://pypi.org/project/zenmoney-mcp-server/)
[![Container](https://img.shields.io/badge/container-ghcr.io%2Fekho%2Fzenmoney--mcp-2496ED?logo=docker&logoColor=white)](https://github.com/ekho/zenmoney-mcp/pkgs/container/zenmoney-mcp)
[![License: MIT](https://img.shields.io/github/license/ekho/zenmoney-mcp.svg)](LICENSE)

[Documentation in English](README.md)

MCP-сервер для надёжной аналитики личных финансов и явно подтверждённых
изменений пользовательских сущностей через API [ZenMoney](https://zenmoney.ru/).
Проект начался как форк [nnslvp/zenmoney-mcp](https://github.com/nnslvp/zenmoney-mcp),
но с тех пор заметно вырос. Он хранит рабочие данные локально, считает финансы
консервативно, синхронизирует данные атомарно и записывает изменения в два этапа.

## Полный каталог инструментов

Локальный и удалённый режимы открывают по 59 инструментов. Из них 57 общие,
ещё по два отвечают за синхронизацию и подбор категорий в конкретном режиме.

| Область | Инструменты |
|---|---|
| Финансовая аналитика | `get_net_worth`, `get_liquidity`, `analyze_spending`, `analyze_income`, `analyze_merchants`, `check_budget_health`, `get_upcoming_payments`, `analyze_trends`, `detect_recurring`, `get_account_flow`, `analyze_transfers`, `detect_anomalies`, `get_debts`, `convert_currency`, `get_exchange_rates`, `search_transactions` |
| Аналитика для планирования | `get_financial_snapshot`, `get_financial_position`, `get_cash_flow`, `get_spending_baseline`, `compare_periods`, `get_emergency_fund_status`, `get_debt_service`, `forecast_cash_flow` |
| Поддержка решений | `plan_emergency_fund`, `plan_debt_payoff`, `compare_debt_strategies`, `plan_financial_goal`, `plan_multiple_goals`, `run_financial_scenario`, `build_financial_plan` |
| Чтение сущностей | `list_accounts`, `get_account`, `list_tags`, `get_tag`, `list_merchants`, `get_merchant`, `list_reminders`, `get_reminder`, `list_reminder_markers`, `get_reminder_marker`, `list_transactions`, `get_transaction`, `list_budgets`, `get_budget` |
| Подтверждённые изменения сущностей | `prepare_account_changes`, `prepare_tag_changes`, `prepare_merchant_changes`, `prepare_reminder_changes`, `prepare_reminder_marker_changes`, `prepare_transaction_changes`, `prepare_budget_changes`, `prepare_changes`, `prepare_mixed_changes`, `prepare_recurring_payment`, `get_change_proposal`, `apply_changes` |

| Режим | Инструменты режима |
|---|---|
| Локальный stdio | `sync_data`, `suggest_category` |
| Удалённый через OpenAI Secure MCP Tunnel | `force_sync`, `get_sync_status` |

## Аналитика

| Вопрос | Инструмент |
|---|---|
| Сколько у меня денег? | `get_net_worth` |
| Могу ли я позволить себе покупку? | `get_liquidity` |
| Куда уходят мои деньги? | `analyze_spending` |
| Откуда приходит мой доход? | `analyze_income` |
| Каким продавцам я плачу? | `analyze_merchants` |
| Укладываюсь ли я в бюджет? | `check_budget_health` |
| Какие у меня есть подписки? | `detect_recurring` |
| Как меняются доходы и расходы? | `analyze_trends` |
| Какие переводы я совершал? | `analyze_transfers` |
| Есть ли необычные или повторяющиеся расходы? | `detect_anomalies` |
| Кто кому должен? | `get_debts` |
| Какие платежи предстоят? | `get_upcoming_payments` |
| Найти подходящие транзакции | `search_transactions` |
| Что происходило на этом счёте? | `get_account_flow` |
| Конвертировать валюты | `convert_currency`, `get_exchange_rates` |
| Активы, обязательства, чистый капитал и свободный денежный поток | `get_financial_position` |
| Старый финансовый снимок с учётом `inBalance` | `get_financial_snapshot` |
| Денежный поток за месяц | `get_cash_flow` |
| Обычный уровень расходов | `get_spending_baseline` |
| Сравнить периоды | `compare_periods` |
| На сколько хватит финансовой подушки | `get_emergency_fund_status` |
| Долговая нагрузка | `get_debt_service` |
| Прогноз на 30/60/90 дней | `forecast_cash_flow` |

Сервер также открывает постраничные коллекции и отдельные ресурсы Account,
Tag, Merchant, Reminder, ReminderMarker, Transaction и Budget. Кроме них доступны
валюты, статус синхронизации и финансовый снимок только из кеша по адресу
`zenmoney://financial-snapshot`. Чтение ресурса никогда не запускает синхронизацию.

### Контракты синхронизации и аномалий

Публичные timestamps синхронизации (`requested_at`, `started_at`, `finished_at`,
`last_sync_time` и planning `data_quality.last_sync`) — строки RFC3339 UTC с
суффиксом `Z`; недоступное значение равно `null`. Кеш и управляющее состояние
по-прежнему хранят целые Unix epoch. `last_server_timestamp` остаётся числовым
cursor дельт ZenMoney, а не timestamp синхронизации.

`detect_anomalies` возвращает ограниченные `exact_duplicates` (один день, сумма
в пользовательской валюте до цента, нормализованный merchant/payee, категория и
outcome account), `same_merchant_amount_close_timestamp` (тот же merchant/payee,
точная сумма и разница до одного дня), `near_duplicates` (тот же нормализованный
merchant/payee и категория, до двух дней и 5% разницы суммы),
`periodic_recurrences` и `unusually_large_one_off`. В кеше есть дата, но нет
времени операции, поэтому сигналы дубликатов содержат `timestamp_precision:
"day"`. Для периодичности читается не более 400 дней истории до конца выбранного
периода, одинаковые category и суммы в пользовательской валюте, округлённые до
цента, группируются по уникальным датам; возвращаются только повторы,
затрагивающие выбранный период: monthly 25–35 дней (минимум 3 события), quarterly
80–100, semiannual 170–195 и annual 350–380 (минимум по 2). ID периодических
операций исключаются из разовых аномалий. Каждый новый список и сохранённые aliases
`outliers` и `possible_duplicates` содержат не более 15 результатов; `summary`
хранит полные счётчики и `results_truncated`.

MCP discovery объявляет для каждого инструмента общую output schema
`{"type":"object"}`. На границе протокола ответ содержит native object в
`structuredContent` и идентичный JSON object в `TextContent` для клиентов,
которым всё ещё нужен текстовый fallback.

## Подтверждённые изменения пользовательских сущностей

Любая запись проходит в два отдельных вызова. Для обычной работы выберите
инструмент подготовки нужной сущности. Если одно предложение создаёт или меняет
несколько связанных типов сущностей, используйте основной `prepare_changes`.
`prepare_mixed_changes` — совместимый alias с той же строгой схемой
`operations[]`:

```text
prepare_account_changes        prepare_tag_changes
prepare_merchant_changes       prepare_reminder_changes
prepare_reminder_marker_changes
prepare_transaction_changes    prepare_budget_changes
prepare_changes                 prepare_mixed_changes (совместимый alias)
prepare_recurring_payment
get_change_proposal            apply_changes
```

Подготовка проверяет от 1 до 100 операций и возвращает неизменяемое превью по
каждому полю, ничего не записывая в ZenMoney. Проверьте его и передайте в
`apply_changes` только `proposal_id`; состояние и результат покажет
`get_change_proposal`.

Перед подготовкой нужна успешная полная синхронизация: так сервер сохранит поля
ZenMoney, которых изменение не касается. При применении сервер синхронизируется
и, если исходная сущность изменилась, отклонит предложение целиком ещё до записи.
Ссылки `{"ref": "..."}` между созданиями разрешаются при подготовке, поэтому одно
предложение соответствует одному mixed-запросу `/v8/diff/`. Это граница
атомарности: при ошибке после отправки результат неизвестен, предложение получит
`needs_review` с `write_result_unknown`, а сервер не повторит запись автоматически.
Повторный `apply_changes` для завершённого предложения ничего не отправляет.

Создание и обновление поддерживаются для всех семи пользовательских сущностей.
При безопасном удалении Account архивируется, Transaction или ReminderMarker
получает отметку удаления, а Budget очищается. Удаление Tag, Merchant и Reminder,
как и физическое стирание любых сущностей, наружу не открыто. Подготовленное
предложение живёт 24 часа, завершённое хранится 30 дней. Если результат записи
или проверки неясен, предложение переходит в состояние `needs_review`.

`prepare_transaction_changes` также умеет делить односторонний доход или расход.
Первая часть сохраняет ID исходной транзакции, остальные получают новые ID. Все
части уходят одним Diff-запросом, сохраняют исходные raw-поля и в сумме должны
точно совпасть с исходной операцией. Одна часть может быть `remainder`, чтобы не
ловить расхождения из-за десятичных дробей:

```json
{
  "operations": [{
    "operation": "split",
    "transaction_id": "transaction-id",
    "parts": [
      {"amount": 730, "category_id": "groceries-category-id"},
      {"amount": "remainder", "category_id": "household-category-id"}
    ]
  }]
}
```

`prepare_recurring_payment` готовит обычный ежемесячный расход и первую
запланированную операцию без записи. Точный payload:

```json
{
  "name": "Кредитка Т-Банк",
  "amount": 28060,
  "account_id": "account-id",
  "category_id": "category-id",
  "frequency": "monthly",
  "day_of_month": 18,
  "start_date": "2026-09-18",
  "end_date": null,
  "notify": true
}
```

Поддерживаются только положительная сумма и `monthly`. `start_date` — ISO-дата,
день которой совпадает с `day_of_month`; `end_date`, если задана, не может быть
раньше. Счёт должен быть активным, а категория — принадлежать его владельцу.
Результат — обычное mixed-предложение из `Reminder` (`interval="month"`,
`step=1`, `points=[0]`) и первого запланированного `ReminderMarker` на
`start_date`. У обеих сущностей одинаковы счёт/instrument, категория, `notify`,
payee и односторонний расход (`income=0`, `outcome=amount`); записать их может
только `apply_changes`.

`get_spending_baseline` использует 3–24 полных бюджетных месяца. При
`include_current_partial_month=true` он добавляет текущий период до даты расчёта
в канонический `monthly_series`; `monthly` остаётся совместимым alias. У
частичной строки `complete=false`, `days_elapsed` и `days_total`, но она не
участвует ни в статистике, ни в поиске периодичности. `trimmed_mean` сортирует
полные значения, отбрасывает с каждого края `floor(n * 10%)`, затем считает
`fmean`.

`expense_patterns` группирует завершённые односторонние операционные расходы по
нормализованному merchant/payee и категории, приведя суммы к валюте пользователя.
Классы: `recurring_monthly` (минимум 3 события, интервалы 25–35 дней),
`likely_quarterly` (минимум 2, 80–100), `likely_semiannual` (минимум 2, 170–195),
`likely_annual` (минимум 2, 350–380), `one_off` (одно событие) и `unknown`.
Для периодических классов разброс суммы не превышает 20% среднего. Это
историческая эвристика, а не прогноз: ответ содержит количество и сводку классов,
возвращает только 100 групп с наибольшей суммой и явно отмечает truncation.

Аналитика для планирования намеренно осторожна:

- для расчёта финансовой подушки нужны явные идентификаторы обязательных
  категорий или заданная вручную сумма обязательных расходов за месяц;
- `get_cash_flow` разделяет операционные расходы, приток заёмных средств и
  денежные платежи по обязательствам;
- `get_debt_service` включает каждый активный счёт с отрицательным балансом
  независимо от `inBalance`;
- `get_financial_position` по той же экономической границе учитывает все
  положительные активы и отрицательные обязательства, а также показывает средние
  операционные потоки и платежи по долгам за три полных месяца;
- неизвестные APR и условия платежей остаются `null`, пока пользователь не
  задаст их явно;
- поиск регулярных платежей опирается на историю и прямо помечен как эвристика;
- прогнозы денежного потока показывают понятные сценарии, а не гарантируют будущее.

`search_transactions` поддерживает стабильную постраничную выдачу через cursor,
сортировку по дате или сумме в валюте пользователя, массивы категорий и счетов,
а также явный фильтр наличия категории. Чтобы получить все расходы без категории
за произвольный период от крупных к мелким, передавайте полученный `next_cursor`
в следующий вызов с теми же фильтрами:

```json
{
  "start_date": "2026-01-01",
  "end_date": "2026-08-31",
  "type": "outcome",
  "category_state": "uncategorized",
  "sort_by": "amount",
  "sort_order": "desc",
  "limit": 50
}
```

## Финансовое планирование

Сервер добавляет детерминированную поддержку решений поверх фактической
аналитики. Каждый результат показывает исходные данные, допущения, ограничения,
причины, альтернативы и измеримые последствия. Само финансовое решение сервер
не исполняет и не записывает.

| Вопрос | Инструмент |
|---|---|
| Как быстро я соберу подушку на 6 месяцев? | `plan_emergency_fund` |
| Стоит ли сначала погасить кредит с высокой ставкой? | `plan_debt_payoff`, `compare_debt_strategies` |
| Могу ли я позволить себе машину через 18 месяцев? | `plan_financial_goal` |
| Какие мои цели конфликтуют? | `plan_multiple_goals` |
| Что случится, если доход упадёт на 20%? | `run_financial_scenario` |
| Как распределить свободный денежный поток за месяц? | `build_financial_plan` |

Параметры планирования, которых нет в ZenMoney, нужно передать явно:

```json
{
  "emergency_fund": {
    "target_months": 6,
    "essential_category_ids": ["category-id"]
  },
  "debt_accounts": {
    "loan-account-id": {
      "apr_pct": 19.9,
      "minimum_payment": 15000
    }
  },
  "goals": []
}
```

Если не заданы APR, минимальные платежи или параметры обязательных расходов,
сервер вернёт `configuration_required`: он не придумывает эти значения.
Расчёты используют нулевую доходность инвестиций, арифметику Decimal для денег
и будущие снимки на конец календарного месяца. Депозиты с ограничениями не
попадают в резерв подушки, пока вы явно их не включите. Кредитный лимит не
учитывается никогда.

План погашения поддерживает четыре явные модели. Для существующего счёта с
отрицательным балансом можно задать `fixed_loan`, `credit_card` или `installment`.
Обязательство, которого нет в ZenMoney, задаётся как `arbitrary` с явным балансом:

```json
{
  "strategy": "avalanche",
  "monthly_extra_payment": 5000,
  "debt_accounts": {
    "loan-account-id": {
      "liability_type": "fixed_loan",
      "apr_pct": 19.9,
      "fixed_payment": 15000
    },
    "card-account-id": {
      "liability_type": "credit_card",
      "apr_pct": 29.9,
      "minimum_payment": 5000,
      "statement_balance": 42000,
      "grace_period_payment": 42000,
      "grace_period_due_date": "2026-09-15"
    },
    "installment-account-id": {
      "liability_type": "installment",
      "payment_schedule": [{"date": "2026-09-30", "amount": 10000}]
    },
    "family-loan": {
      "liability_type": "arbitrary",
      "title": "Семейный долг",
      "balance": 50000,
      "minimum_payment": 5000
    }
  }
}
```

Политика приоритетов, формулы, округление, метки качества данных и ограничения
описаны в [`docs/planning-semantics.md`](docs/planning-semantics.md).

## Режимы работы

Установленная команда `zenmoney-mcp` запускает локальный stdio-сервер для Codex,
ChatGPT Desktop, Claude Desktop и Cursor. Он напрямую использует общий реестр
SDK v2 и усиленную среду выполнения, без оверлея над исходным сервером.

В частном удалённом развёртывании `zenmoney-mcp-http` открывает Streamable HTTP
по адресу `/mcp` только внутри Docker, а клиент OpenAI Secure MCP Tunnel сам
подключается к OpenAI. В удалённом реестре нет локальных инструментов
`sync_data` и `suggest_category`, зависящих от API. Аналитические инструменты
остаются доступными только для чтения. Удалённый `force_sync` запрашивает
обновление кеша, а подтверждённые предложения изменений ставятся в очередь для
отдельного воркера с учётными данными. Ход этих операций показывают
`get_sync_status` и `get_change_proposal`. MCP-контейнер по-прежнему не получает
токен ZenMoney, не может записывать финансовый снимок и не обращается к ZenMoney
напрямую. Подробности есть в [инструкции по удалённой эксплуатации](deploy/remote-mcp/README.md)
и [модели угроз](docs/remote-mcp-threat-model.md).

`force_sync(force_full=false, wait_until_complete=false)` по умолчанию
асинхронен. При `wait_until_complete=true` он ждёт только тот же request ID,
опрашивая валидированное управляющее состояние каждые 0,25 с не более фиксированных
60 с. Terminal result — `completed` или `failed`; timeout возвращает
`status: "timeout"`, текущее состояние `pending` или `running` и
`wait_timed_out: true`, не отменяя воркер. Некорректное или заменённое состояние
fail closed с `invalid_sync_state`; pending или running request остаётся
single-flight.

## Усиления в этом форке

Установленная команда `zenmoney-mcp` запускает `zenmoney_mcp.entrypoint` с общим
реестром SDK v2 и усиленными реализациями базы данных, синхронизации и аналитики:

- `HardenedDatabase` добавляет идемпотентные миграции и строгую работу с
  валютными курсами;
- `HardenedSyncEngine` проверяет ответы и атомарно заменяет рабочий кеш;
- исправленная аналитика охватывает чистый капитал, ликвидность, бюджеты, долги,
  движение по счёту, расходы, поиск транзакций, предстоящие платежи и валютные
  курсы;
- остальная аналитика проверяется в заданных пределах во время выполнения;
- список MCP-инструментов показывает те же лимиты, которые проверяются при работе.

Основные правила расчётов:

- `net_worth` учитывает только активные счета с `in_balance=true`;
- исключённые счета возвращаются отдельно в `net_worth_all_accounts`;
- кредит означает возможность занять деньги, а не актив;
- доступные накопления и срочные депозиты считаются разной ликвидностью;
- бюджетные периоды учитывают заданный пользователем день начала месяца;
- нулевой бюджет и расходы вне бюджета показываются явно;
- активный отрицательный баланс считается обязательством независимо от
  `inBalance`;
- остатки на долговых счетах имеют приоритет, а пробелы в
  атрибуции остаются видимыми;
- движение по счёту включает переводы со знаком в валюте счёта и пользователя;
- отсутствующий или нулевой курс вызывает явную ошибку, а не превращается в 1:1;
- полная синхронизация заменяет кеш целиком, поэтому старые строки не остаются.

Подробности описаны в [`README-HARDENING.md`](README-HARDENING.md).

## Локальная установка через uvx

Установите [uv](https://docs.astral.sh/uv/getting-started/installation/), затем
запустите сервер из PyPI.

Получите личный API-токен на [zerro.app/token](https://zerro.app/token), как
описано в [официальной wiki API ZenMoney](https://github.com/zenmoney/ZenPlugins/wiki/ZenMoney-API).

```bash
export ZENMONEY_TOKEN="replace-with-your-token"
uvx --from zenmoney-mcp-server zenmoney-mcp
```

`uvx` скачивает пакет в изолированное окружение и кеширует его. Клонировать
репозиторий и создавать виртуальное окружение не нужно.

Чтобы запустить текущую ветку `main` до следующей публикации в PyPI, выполните
`uvx --from git+https://github.com/ekho/zenmoney-mcp.git zenmoney-mcp`.

При первом запуске усиленной версии выполняются аддитивные миграции SQLite.
Если вам важно сохранить существующий кеш, заранее сделайте резервную копию
`~/.cache/zenmoney-mcp/zenmoney.db`. Полная синхронизация умеет пересоздать кеш
из ZenMoney.

## Частное подключение ChatGPT через OpenAI Secure MCP Tunnel

ChatGPT в браузере не умеет запускать локальную stdio-команду. Для частного
удалённого доступа запустите готовое развёртывание Docker Compose и подключите
его через [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
Конечная точка MCP остаётся внутри сети Docker. Исходящее соединение с OpenAI
устанавливает только клиент туннеля.

Для этого режима нужны Docker Engine с Compose v2, токен ZenMoney, API-ключ OpenAI
для работы туннеля и идентификатор туннеля, связанный с нужным рабочим
пространством ChatGPT. Клонируйте репозиторий и создайте файл окружения без
секретов:

```bash
git clone https://github.com/ekho/zenmoney-mcp.git ~/zenmoney-mcp
cd ~/zenmoney-mcp
cp deploy/remote-mcp/.env.example deploy/remote-mcp/.env
```

Укажите `CONTROL_PLANE_TUNNEL_ID` в `deploy/remote-mcp/.env`. Токен ZenMoney и
ключ OpenAI сохраните как отдельные файловые секреты Compose. Команды для
назначения владельца и прав приведены в
[инструкции по удалённой эксплуатации](deploy/remote-mcp/README.md). Не кладите
секреты в `.env`. Затем загрузите образы и запустите развёртывание:

```bash
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml pull
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml up -d --no-build --pull never
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml ps
```

Выполните проверки состояния и `tunnel-client doctor` из инструкции, затем
добавьте MCP-приложение в ChatGPT Developer Mode, выберите **Connection = Tunnel**
и запустите сканирование инструментов.

## ChatGPT Desktop и Codex

Добавьте сервер в `~/.codex/config.toml`:

```toml
[mcp_servers.zenmoney]
command = "uvx"
args = ["--from", "git+https://github.com/ekho/zenmoney-mcp.git", "zenmoney-mcp"]
env_vars = ["ZENMONEY_TOKEN"]
tool_timeout_sec = 120
```

После изменения конфигурации MCP перезапустите настольный клиент. Для ChatGPT
в браузере используйте частное удалённое развёртывание Streamable HTTP + Secure
MCP Tunnel из [инструкции по эксплуатации](deploy/remote-mcp/README.md), а не
локальный исполняемый файл.

## Claude Desktop или Cursor

```json
{
  "mcpServers": {
    "zenmoney": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/ekho/zenmoney-mcp.git",
        "zenmoney-mcp"
      ],
      "env": {
        "ZENMONEY_TOKEN": "replace-with-your-token"
      }
    }
  }
}
```

## Разработка

Клонируйте репозиторий, только если собираетесь менять или локально проверять код:

```bash
git clone https://github.com/ekho/zenmoney-mcp.git ~/zenmoney-mcp
cd ~/zenmoney-mcp
uv sync --extra dev
```

## Поток данных

1. Локальный `sync_data` читает `/v8/diff/` напрямую через локальный механизм
   синхронизации.
2. В удалённом развёртывании периодический воркер читает `/v8/diff/`, а удалённый
   `force_sync` лишь просит воркер с учётными данными запуститься сразу.
3. В обоих режимах SQLite-кеш публикуется по адресу
   `~/.cache/zenmoney-mcp/zenmoney.db` или заданному `ZENMONEY_DB_PATH`. Аналитика
   читает этот кеш локально.
4. Локальное подтверждённое предложение записывается синхронно. Удалённое
   предложение сохраняется в управляющем томе, а затем его записывает воркер.
5. Обращаться к ZenMoney умеют только локальный процесс и воркер с учётными
   данными.

## Тестирование

```bash
uv sync --extra dev
uv run python -m compileall -q src tests
uv run python -m pytest tests/ -v --ignore=tests/test_integration.py
```

Для интеграционного теста нужен `ZENMONEY_TOKEN`, поэтому в CI он не запускается.

## Лицензия

MIT
