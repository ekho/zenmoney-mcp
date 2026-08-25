# ZenMoney MCP Server

Python 3.11+ MCP server for personal-finance analytics and confirmed changes to
ZenMoney user entities. It supports local stdio and a private remote HTTP
deployment whose MCP container has no ZenMoney credential.

## Stack and commands

- `mcp>=2,<3`, `httpx`, stdlib `sqlite3`; do not add pandas, SQLAlchemy, or an ORM.
- Use `uv run python` and `uv run pytest`.
- Local entrypoint: `zenmoney-mcp`.
- Remote entrypoints: `zenmoney-mcp-http`, `zenmoney-sync-worker`, and
  `zenmoney-sync-once`.

## Main modules

- `server.py`: shared tool and resource registry.
- `hardened_database.py`: normalized cache plus canonical raw user entities.
- `hardened_sync.py`: validated atomic `/v8/diff/` synchronization and writes.
- `entity_changes.py`: strict create/update/safe-delete validation.
- `mutations.py`: private two-step proposal ledger and executor.
- `entity_resources.py`: paginated collection and exact entity resources.
- `sync_worker.py`: credentialed remote sync and proposal worker.
- `analytics.py`, `financial_correctness.py`, `planning.py`: read and planning
  logic.

## Mutation contract

User entities are Account, Tag, Merchant, Reminder, ReminderMarker,
Transaction, and Budget. Instrument, Company, and User remain read-only.

Every write is `prepare -> review -> apply`. Seven entity-specific
`prepare_*_changes` tools and `prepare_mixed_changes` produce immutable
proposals; `get_change_proposal` reads them and `apply_changes` confirms one.
The remote MCP only queues apply; the credentialed worker performs it.

Create and update are supported for all seven types. Safe delete is limited to
Account archive, Transaction and ReminderMarker semantic deletion, and Budget
clearing. Never expose physical `deletion`/purge without a new approved design.

Preserve complete raw objects and unknown fields. Reject stale `changed`
versions before sending anything. Related creates are sent in dependency layers,
one mixed Diff request per layer. Never retry or roll back an ambiguous write;
finish it as `needs_review`. Verify writes with a full sync because a deleted
ReminderMarker can disappear from the full response without an incremental
tombstone.

## Analytical invariants

- Exclude deleted and held transactions where the query requires settled facts.
- Treat rows with both `income > 0` and `outcome > 0` as transfers, not ordinary
  income or spending.
- Convert currencies before aggregation:
  `amount_user = amount * instrument.rate / user_currency.rate`.
- Parent-tag queries include children.
- Keep responses bounded and enrich identifiers through SQL joins where
  practical.

## API and testing

The upstream wiki is useful but stale; live writes require a dedicated test
profile and the fail-closed harness in `tests/live_entity_changes.py`.

Reference: <https://github.com/zenmoney/ZenPlugins/wiki/ZenMoney-API>

Before committing, run the complete non-live suite:

```bash
uv run pytest -q
```

The live harness requires an explicit external mode-`0600` token file and exact
test owner ID. Never print tokens, response bodies, account names, or financial
amounts from it.
