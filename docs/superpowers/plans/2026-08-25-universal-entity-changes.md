# Universal ZenMoney User-Entity Changes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing confirmed transaction mutation branch into safe create, read, update, and semantic-delete workflows for all seven ZenMoney user entities.

**Architecture:** Keep normalized SQLite tables for analytics and add one canonical raw-object table for write fidelity. Replace the transaction-specific proposal code with one entity-neutral ledger and executor, while keeping validation in a small fixed registry. MCP exposes entity-specific prepare tools, generic get/apply tools, and paginated entity resources; remote apply still queues work for the credentialed worker.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`/`json`/`uuid`/`base64`, existing `httpx`, MCP Python SDK 2, pytest.

**Spec:** `docs/superpowers/specs/2026-08-25-universal-entity-crud-design.md`

## Global Constraints

- User entities are `account`, `tag`, `merchant`, `reminder`, `reminderMarker`, `transaction`, and `budget`; system entities remain read-only.
- Every write is prepare -> review -> apply and contains one to 100 frozen operations.
- One proposal produces the minimum ordered dependency layers; each layer is one
  mixed `/v8/diff/` request, with no retry or rollback.
- Create IDs come from lowercase `str(uuid.uuid4())` and are frozen during prepare; the live gate must prove this format for every UUID-backed type before delivery.
- Account `balance` is immutable and `startBalance` is create-only.
- Safe delete is limited to account archive, transaction deletion, reminder-marker deletion, and budget clear; `purge` is absent.
- Preserve complete upstream objects and fail closed on fields outside fixed allowlists.
- Keep the remote MCP container free of the ZenMoney token and direct ZenMoney egress.
- Use stdlib and current dependencies only. Do not add an ORM, queue, cursor library, or schema framework.
- Never log tokens, raw objects, Diff bodies, financial values, or proposal previews.

---

### Task 1: Canonical raw snapshot for all user entities

**Files:**
- Modify: `src/zenmoney_mcp/hardened_database.py`
- Modify: `src/zenmoney_mcp/hardened_sync.py`
- Test: `tests/test_hardened_database.py`
- Test: `tests/test_hardened_sync.py`

**Interfaces:**
- Produces: `entity_key(entity_type: str, value: dict[str, Any]) -> str`
- Produces: `HardenedDatabase.get_entity_raw(entity_type: str, key: str) -> dict[str, Any] | None`
- Produces: `HardenedDatabase.user_entity_mutations_ready() -> bool`
- Produces: `sync_meta.user_entity_raw_complete`, set only after successful full publication.

- [ ] **Step 1: Write failing raw-merge and identity tests**

```python
def test_raw_user_entities_merge_partial_objects():
    db = HardenedDatabase(":memory:")
    db.init_schema()
    db.upsert_tags([{"id": "tag", "user": 1, "changed": 1,
                     "title": "Food", "future": {"x": 1}}])
    db.upsert_tags([{"id": "tag", "changed": 2, "title": "Dining"}])
    assert db.get_entity_raw("tag", '"tag"') == {
        "id": "tag", "user": 1, "changed": 2,
        "title": "Dining", "future": {"x": 1},
    }

def test_budget_raw_identity_is_canonical_composite_key():
    assert entity_key("budget", {"user": 1, "date": "2026-08-01", "tag": None}) == (
        '{"date":"2026-08-01","tag":null,"user":1}'
    )
```

Also cover all seven upsert methods, transaction `raw_json` migration, deletion cleanup, and a failed full publication leaving `user_entity_raw_complete` unset.

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
uv run pytest tests/test_hardened_database.py tests/test_hardened_sync.py -q
```

Expected: FAIL because `entity_raw`, `entity_key`, and generic raw access do not exist.

- [ ] **Step 3: Add schema v4 and one raw-upsert helper**

Create:

```sql
CREATE TABLE IF NOT EXISTS entity_raw (
    entity_type TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_key)
)
```

In `HardenedDatabase`, route the seven existing upsert overrides through:

```python
def _merge_entity_raw(self, entity_type, items):
    return [
        {**(self.get_entity_raw(entity_type, entity_key(entity_type, item)) or {}), **item}
        for item in items
    ]
```

Write normalized rows first, then canonical compact raw JSON in the same connection. Keep the old transaction column for compatibility but stop reading it. Increment `SCHEMA_VERSION` to `4`, include `entity_raw` in snapshot validation, and set `user_entity_raw_complete=1` on full staging snapshots only.

- [ ] **Step 4: Run snapshot regressions**

```bash
uv run pytest tests/test_hardened_database.py tests/test_hardened_sync.py tests/test_database.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zenmoney_mcp/hardened_database.py src/zenmoney_mcp/hardened_sync.py tests/test_hardened_database.py tests/test_hardened_sync.py
git commit -m "feat: preserve raw user entities" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 2: Fixed entity validation and proposal-local references

**Files:**
- Create: `src/zenmoney_mcp/entity_changes.py`
- Create: `tests/test_entity_changes.py`

**Interfaces:**
- Produces: `ENTITY_TYPES`, `DIFF_FIELDS`, `UUID_ENTITY_TYPES`
- Produces: `normalize_operations(db, operations, entity_type=None, now=None) -> list[dict[str, Any]]`
- Produces: `rebuild_after(db, item, raw) -> dict[str, Any]`
- Produces: `verify_after(item, raw) -> bool`
- Produces: `MutationValidationError` and `MutationStateError`.

- [ ] **Step 1: Write failing validator tests**

```python
def test_mixed_create_resolves_typed_refs_and_owner(financial_db):
    items = normalize_operations(financial_db, [
        {"entity": "tag", "operation": "create", "ref": "food",
         "value": {"title": "MCP TEST Food", "showIncome": False,
                   "showOutcome": True, "budgetIncome": False,
                   "budgetOutcome": True}},
        {"entity": "transaction", "operation": "create", "ref": "lunch",
         "value": {"date": "2026-08-25", "income": 0, "outcome": 10,
                   "incomeAccount": "cash", "outcomeAccount": "cash",
                   "incomeInstrument": 1, "outcomeInstrument": 1,
                   "tag": [{"ref": "food"}]}}
    ], now=100)
    assert items[1]["resolved"]["tag"] == [items[0]["entity_id"]]
    assert items[0]["resolved"]["user"] == items[1]["resolved"]["user"] == 1
```

Add parameterized failures for unknown fields, missing and duplicate refs, wrong ref type, cycles, duplicate identities, cross-owner references, multiple users without `owner_user_id`, immutable fields, invalid dates/numbers/enums, account `balance`, update of `startBalance`, and unsupported delete types.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/test_entity_changes.py -q
```

Expected: import failure for `entity_changes`.

- [ ] **Step 3: Implement the fixed registry**

Use one dict per entity, not classes. Exact editable fields:

```python
EDITABLE = {
    "account": {"title", "type", "instrument", "company", "role", "syncID",
                "creditLimit", "inBalance", "savings", "enableCorrection",
                "enableSMS", "capitalization", "percent", "startDate",
                "endDateOffset", "endDateOffsetInterval", "payoffStep", "payoffInterval"},
    "tag": {"title", "parent", "icon", "picture", "color", "showIncome",
            "showOutcome", "budgetIncome", "budgetOutcome", "required"},
    "merchant": {"title"},
    "reminder": {"incomeInstrument", "incomeAccount", "income", "outcomeInstrument",
                 "outcomeAccount", "outcome", "tag", "merchant", "payee", "comment",
                 "interval", "step", "points", "startDate", "endDate", "notify"},
    "reminderMarker": {"incomeInstrument", "incomeAccount", "income",
                       "outcomeInstrument", "outcomeAccount", "outcome", "tag",
                       "merchant", "payee", "comment", "date", "reminder", "state",
                       "notify"},
    "transaction": {"date", "income", "outcome", "incomeAccount", "outcomeAccount",
                    "incomeInstrument", "outcomeInstrument", "tag", "merchant", "payee",
                    "comment", "opIncome", "opOutcome", "opIncomeInstrument",
                    "opOutcomeInstrument", "latitude", "longitude"},
    "budget": {"income", "incomeLock", "outcome", "outcomeLock"},
}
```

`startBalance` is accepted only inside account create values; the outgoing
create object derives `balance` from it, so callers never set `balance`
directly. Add `id`, `user`, `changed`, and transaction `created` during
preparation. Resolve refs in input order, rejecting forward refs so no graph
sorter is needed. Safe delete rewrites the fixed fields from the spec.

- [ ] **Step 4: Run validator tests**

```bash
uv run pytest tests/test_entity_changes.py tests/test_mutations.py -q
```

Expected: new tests PASS; old transaction tests still pass until the ledger replacement.

- [ ] **Step 5: Commit**

```bash
git add src/zenmoney_mcp/entity_changes.py tests/test_entity_changes.py
git commit -m "feat: validate user entity changes" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 3: Entity-neutral proposal ledger and dependency-layer executor

**Files:**
- Rename: `src/zenmoney_mcp/transaction_mutations.py` -> `src/zenmoney_mcp/mutations.py`
- Replace: `tests/test_transaction_mutations.py` -> `tests/test_mutations.py`
- Modify: `src/zenmoney_mcp/hardened_sync.py`

**Interfaces:**
- Produces: `ProposalStore` with the existing lifecycle methods and generic items.
- Produces: `prepare_changes(db, store, operations, entity_type=None, now=None) -> dict[str, Any]`
- Produces: `get_change_proposal(store, proposal_id, now=None) -> dict[str, Any]`
- Produces: `execute_proposal(db, engine, store, proposal_id, now=None) -> Awaitable[dict[str, Any]]`
- Produces: `HardenedSyncEngine.push_changes(changes: dict[str, list[dict[str, Any]]]) -> dict[str, Any]`.

- [ ] **Step 1: Replace transaction-only tests with failing generic tests**

```python
@pytest.mark.asyncio
async def test_mixed_executor_sends_one_request_and_verifies_all_types(financial_db, tmp_path):
    store = ProposalStore(tmp_path / "proposals.db")
    prepared = prepare_changes(financial_db, store, mixed_operations(), now=90)
    engine = SuccessfulMixedEngine(financial_db)
    result = await execute_proposal(financial_db, engine, store,
                                    prepared["proposal_id"], now=100)
    assert result["status"] == "applied"
    assert len(engine.pushed) == 1
    assert set(engine.pushed[0]) == {"tag", "transaction"}
```

Retain lifecycle, file-mode, expiry, cleanup, idempotency, conflict, ambiguous transport, failed verification, and restart recovery coverage. Add create collision, mixed preflight rejection with zero write calls, dependency-layer ordering, and partial-layer failure without retry.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/test_mutations.py tests/test_hardened_sync.py -q
```

Expected: generic module and methods are missing.

- [ ] **Step 3: Generalize the ledger without compatibility aliases**

Rename any old `proposals` and `proposal_items` tables to `proposals_transaction_v1` and `proposal_items_transaction_v1` once, then create the generic schema from the spec. Public items use `entity`, `key`, `operation`, `expected_changed`, `changes`, and `result`; full raw objects never enter the proposal database.

Implement `push_changes` by validating non-empty keys against `DIFF_FIELDS`, adding every entity array for one layer to one body, calling `_post_diff` once, and applying the response through staging. The executor performs preflight sync, whole-proposal concurrency checks, rebuild, ordered dependency-layer pushes, a full verification sync, and fixed terminal results. Any uncertainty after submission becomes `needs_review`.

- [ ] **Step 4: Run mutation and sync regressions**

```bash
uv run pytest tests/test_mutations.py tests/test_entity_changes.py tests/test_hardened_sync.py tests/test_sync_worker.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zenmoney_mcp/mutations.py src/zenmoney_mcp/hardened_sync.py tests/test_mutations.py tests/test_hardened_sync.py
git add -u src/zenmoney_mcp/transaction_mutations.py tests/test_transaction_mutations.py
git commit -m "feat: execute mixed entity changes" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 4: Paginated entity resources

**Files:**
- Create: `src/zenmoney_mcp/entity_resources.py`
- Create: `tests/test_entity_resources.py`
- Modify: `src/zenmoney_mcp/server.py`

**Interfaces:**
- Produces: `list_entity_resource(db, entity_type, limit=50, cursor=None, include_inactive=False) -> dict[str, Any]`
- Produces: `get_entity_resource(db, entity_type, key_parts) -> dict[str, Any]`
- Produces: `encode_cursor(sort_key: list[Any]) -> str` and strict `decode_cursor`.

- [ ] **Step 1: Write failing resource tests**

```python
def test_collection_cursor_is_stable_and_opaque(financial_db):
    first = list_entity_resource(financial_db, "transaction", limit=1)
    second = list_entity_resource(financial_db, "transaction", limit=1,
                                  cursor=first["next_cursor"])
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert "tx" not in first["next_cursor"]
```

Cover default 50, maximum 200, invalid cursors, inactive filtering, exact inactive reads, budget composite keys, deterministic order, normalized output, and no `raw_json` leakage.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/test_entity_resources.py -q
```

Expected: import failure.

- [ ] **Step 3: Implement SQL keyset pagination**

Use base64url-encoded canonical JSON cursors and parameterized keyset queries. Keep fixed per-entity SELECT/WHERE/ORDER BY definitions in a module constant. Reuse current public resource field names where an old collection already exists; add exact resources and missing reminder-marker, reminder, transaction, tag, and budget collections.

- [ ] **Step 4: Run resource regressions**

```bash
uv run pytest tests/test_entity_resources.py tests/test_tools.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zenmoney_mcp/entity_resources.py src/zenmoney_mcp/server.py tests/test_entity_resources.py tests/test_tools.py
git commit -m "feat: expose user entity resources" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 5: MCP tools, remote queue, and worker wiring

**Files:**
- Modify: `src/zenmoney_mcp/server.py`
- Modify: `src/zenmoney_mcp/sync_worker.py`
- Modify: `src/zenmoney_mcp/entrypoint.py`
- Modify: `tests/test_entrypoint.py`
- Modify: `tests/test_remote_http.py`
- Modify: `tests/test_sync_worker.py`

**Interfaces:**
- Exposes the ten mutation tools named in the spec.
- Exposes collection and exact resource templates named in the spec.
- Local apply calls `execute_proposal`; remote apply only calls `ProposalStore.request_apply`.

- [ ] **Step 1: Write failing public-surface tests**

```python
@pytest.mark.asyncio
async def test_mutation_tool_names_are_entity_specific():
    names = {tool.name for tool in await list_tools(remote=False)}
    assert MUTATION_TOOL_NAMES <= names
    assert "apply_transaction_changes" not in names
    assert "get_transaction_change_proposal" not in names
```

Add strict schema assertions for every prepare tool, mixed `entity` discriminators, tool annotations, sanitized rejection codes, local execution, remote queueing, worker draining, and remote-token separation.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/test_entrypoint.py tests/test_remote_http.py tests/test_sync_worker.py -q
```

Expected: old transaction-only names remain and imports still target the old module.

- [ ] **Step 3: Wire the generic core**

Build seven schemas from fixed property dicts and one mixed union; do not hand-copy dispatch branches. Map tool name to entity type in one constant. The prepare dispatch calls `prepare_changes`; get/apply use one proposal-ID schema. Replace worker imports and call `execute_proposal`. Register MCP resource-template handlers using the installed SDK types.

- [ ] **Step 4: Run server and topology regressions**

```bash
uv run pytest tests/test_entrypoint.py tests/test_remote_http.py tests/test_sync_worker.py tests/test_remote_deployment.py tests/test_tools.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zenmoney_mcp/server.py src/zenmoney_mcp/sync_worker.py src/zenmoney_mcp/entrypoint.py tests/test_entrypoint.py tests/test_remote_http.py tests/test_sync_worker.py tests/test_remote_deployment.py tests/test_tools.py
git commit -m "feat: expose confirmed entity changes" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 6: Disposable-profile live capability gate

**Files:**
- Create: `tests/live_entity_changes.py`
- Modify: `README.md`
- Modify: `deploy/remote-mcp/README.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Command: `ZENMONEY_TEST_TOKEN_FILE=... ZENMONEY_TEST_USER_ID=... uv run python tests/live_entity_changes.py`
- Output: entity IDs, proposal states, and fixed error codes only.

- [ ] **Step 1: Write the fail-closed harness self-tests**

Put pure configuration checks in `tests/test_live_entity_changes.py`:

```python
def test_live_config_requires_external_file_and_numeric_owner(tmp_path, monkeypatch):
    monkeypatch.delenv("ZENMONEY_TEST_USER_ID", raising=False)
    with pytest.raises(ValueError, match="ZENMONEY_TEST_USER_ID"):
        live_config()
```

Also reject missing files, repository-relative paths, modes broader than `0600`, empty tokens, multiple owners, and a mismatched owner before any write.

- [ ] **Step 2: Run config tests and verify RED**

```bash
uv run pytest tests/test_live_entity_changes.py -q
```

Expected: helper module is missing.

- [ ] **Step 3: Implement three live proposals**

The script performs full sync, checks the one configured owner, creates the
seven-type dependency graph with names formatted as
`f"MCP TEST {int(time.time())}"`, updates all seven types, then safe-deletes
account/transaction/reminderMarker/budget. Stop immediately on any non-applied
state. Do not catch and print upstream exception strings.

- [ ] **Step 4: Run non-live tests, then the authorized test-profile gate**

```bash
uv run pytest tests/test_live_entity_changes.py -q
ZENMONEY_TEST_TOKEN_FILE=/Users/ekho/.config/zenmoney-mcp/test-token \
ZENMONEY_TEST_USER_ID="$ZENMONEY_TEST_USER_ID" \
uv run python tests/live_entity_changes.py
```

Expected: three proposals report `applied`; no token or financial values appear. If UUID creation fails, stop delivery and record the fixed failure code instead of trying alternative IDs against the API.

- [ ] **Step 5: Commit**

```bash
git add tests/live_entity_changes.py tests/test_live_entity_changes.py README.md deploy/remote-mcp/README.md CLAUDE.md
git commit -m "test: verify live entity changes" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 7: Full verification and PR update

**Files:**
- Modify if required by checks: files already listed above.

- [ ] **Step 1: Run the complete non-live suite**

```bash
uv run pytest tests/ -q --ignore=tests/test_integration.py
```

Expected: PASS with no skipped non-live mutation tests.

- [ ] **Step 2: Run package and deployment checks**

```bash
uv build
docker build -t zenmoney-mcp:entity-changes .
docker compose -f deploy/remote-mcp/compose.yaml config --quiet
git diff --check origin/main...HEAD
```

Expected: all commands exit `0`.

- [ ] **Step 3: Inspect the final diff and secret boundary**

```bash
git status --short
git diff --stat origin/main...HEAD
rg -n "ZENMONEY_TOKEN|test-token|wTX994Zbs3oxFcm" . \
  --glob '!docs/superpowers/**' --glob '!uv.lock'
```

Expected: no secret value, share URL, or token path is committed; only documented variable names remain.

- [ ] **Step 4: Commit any verification-only corrections**

```bash
git add -u
git commit -m "fix: complete entity change verification" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

Skip this commit when the tree is already clean.

- [ ] **Step 5: Verify target and push the explicit branch**

```bash
git remote get-url origin
git rev-parse HEAD
git push origin HEAD:refs/heads/codex/transaction-mutations
```

Then confirm PR #8 head SHA and CI status. Do not merge or deploy.
