# Transaction Mutations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent two-step batch editing of existing ZenMoney transactions to local stdio MCP and the private remote worker topology.

**Architecture:** Preserve every upstream transaction as raw JSON in the analytical snapshot, store immutable proposals in a separate SQLite ledger, and execute confirmed proposals through one mutation executor. Local stdio runs the executor synchronously; remote MCP only queues work for the credentialed sync worker.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`/`json`/`uuid`, existing `httpx`, MCP Python SDK 2, pytest.

**Spec:** `docs/superpowers/specs/2026-08-25-transaction-mutations-design.md`

## Global Constraints

- Keep the remote MCP container free of `ZENMONEY_TOKEN` and direct ZenMoney egress.
- Use only stdlib SQLite and existing dependencies; do not add an ORM or queue service.
- Mutation proposals contain exact transaction IDs and immutable patches, never dynamic selectors.
- Allow batches of one to 100 transactions.
- Only `deleted: false -> true` is supported; restoration is out of scope.
- Prepared proposals expire after 24 hours; terminal rows are retained for 30 days.
- Never log tokens, raw upstream bodies, transaction objects, or proposal previews.
- Automated tests use no live ZenMoney credential; the disposable-transaction live gate stays manual and separately authorized.

---

### Task 1: Full-fidelity transaction snapshot

**Files:**
- Modify: `src/zenmoney_mcp/database.py`
- Modify: `src/zenmoney_mcp/hardened_database.py`
- Modify: `src/zenmoney_mcp/hardened_sync.py`
- Test: `tests/test_hardened_database.py`
- Test: `tests/test_hardened_sync.py`

**Interfaces:**
- Produces: `HardenedDatabase.get_transaction_raw(transaction_id: str) -> dict[str, Any] | None`
- Produces: `HardenedDatabase.transaction_mutations_ready() -> bool`
- Produces: `sync_meta.transaction_raw_complete`, set only by successful full sync.

- [ ] **Step 1: Write failing migration and preservation tests**

```python
def test_transaction_raw_json_preserves_unknown_fields_across_partial_diff():
    db = HardenedDatabase(":memory:")
    db.init_schema()
    db.upsert_transactions([{
        "id": "tx", "user": 1, "changed": 10, "created": 1,
        "date": "2026-08-25", "income": 0, "outcome": 10,
        "incomeAccount": "cash", "outcomeAccount": "cash",
        "incomeInstrument": 1, "outcomeInstrument": 1,
        "deleted": False, "source": "bank", "futureField": {"x": 1},
    }])
    db.upsert_transactions([{"id": "tx", "changed": 11, "comment": "fixed"}])

    assert db.get_transaction_raw("tx") == {
        "id": "tx", "user": 1, "changed": 11, "created": 1,
        "date": "2026-08-25", "income": 0, "outcome": 10,
        "incomeAccount": "cash", "outcomeAccount": "cash",
        "incomeInstrument": 1, "outcomeInstrument": 1,
        "deleted": False, "source": "bank", "futureField": {"x": 1},
        "comment": "fixed",
    }
```

```python
def test_only_full_sync_enables_transaction_mutations():
    db = base_db()
    engine = HardenedSyncEngine(db, "token")
    engine.apply_diff_data(full_snapshot(), force_full=False)
    assert db.transaction_mutations_ready() is False

    engine.apply_diff_data(full_snapshot(), force_full=True)
    assert db.transaction_mutations_ready() is True
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest tests/test_hardened_database.py tests/test_hardened_sync.py -q
```

Expected: FAIL because `raw_json`, `get_transaction_raw`, and `transaction_mutations_ready` do not exist.

- [ ] **Step 3: Add schema v3 and merged raw persistence**

Add `raw_json TEXT` to the transaction schema, increment `SCHEMA_VERSION` to 3,
and add the migration through `_add_column`. Override the hardened transaction
upsert so incoming fields merge over the previous raw object before the base
upsert writes normalized fields and canonical compact JSON.

```python
def get_transaction_raw(self, transaction_id: str) -> dict[str, Any] | None:
    row = self.connect().execute(
        "SELECT raw_json FROM transactions WHERE id = ?", (transaction_id,)
    ).fetchone()
    if row is None or row["raw_json"] is None:
        return None
    value = json.loads(row["raw_json"])
    return value if isinstance(value, dict) else None

def transaction_mutations_ready(self) -> bool:
    return self.get_meta("transaction_raw_complete") == "1"
```

In `apply_diff_data`, set `transaction_raw_complete=1` on the staging database
only for `force_full=True`, before publishing the staging snapshot.

- [ ] **Step 4: Run focused and database regression tests**

```bash
uv run pytest tests/test_hardened_database.py tests/test_hardened_sync.py tests/test_database.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the snapshot layer**

```bash
git add src/zenmoney_mcp/database.py src/zenmoney_mcp/hardened_database.py src/zenmoney_mcp/hardened_sync.py tests/test_hardened_database.py tests/test_hardened_sync.py
git commit -m "feat: preserve full transaction payloads" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 2: Persistent proposal ledger and validation

**Files:**
- Create: `src/zenmoney_mcp/transaction_mutations.py`
- Create: `tests/test_transaction_mutations.py`

**Interfaces:**
- Produces: `ProposalStore(path: str | Path)` with `create`, `get`, `request_apply`, `claim`, `finish`, `recover_running`, and `close`.
- Produces: `prepare_transaction_changes(db, store, changes, now=None) -> dict[str, Any]`
- Produces: `get_transaction_change_proposal(store, proposal_id, now=None) -> dict[str, Any]`
- Produces: `validate_transaction_patch(db, raw, patch) -> dict[str, Any]`

- [ ] **Step 1: Write failing proposal behavior tests**

Create real in-memory financial data and a temporary proposal database. Tests
must prove these observable contracts:

```python
def test_prepare_persists_immutable_bounded_preview(financial_db, tmp_path):
    store = ProposalStore(tmp_path / "proposals.db")
    result = prepare_transaction_changes(
        financial_db,
        store,
        [{"transaction_id": "tx", "set": {"tag": ["food"], "comment": "fixed"}}],
        now=1_000,
    )

    assert result["status"] == "prepared"
    assert result["created_at"] == 1_000
    assert result["expires_at"] == 87_400
    assert result["items"] == [{
        "transaction_id": "tx",
        "expected_changed": 10,
        "changes": {
            "comment": {"before": None, "after": "fixed"},
            "tag": {"before": [], "after": ["food"]},
        },
        "result": None,
    }]
```

```python
def test_prepare_rejects_duplicate_ids_and_forbidden_fields(financial_db, tmp_path):
    store = ProposalStore(tmp_path / "proposals.db")
    with pytest.raises(MutationValidationError, match="duplicate"):
        prepare_transaction_changes(financial_db, store, [
            {"transaction_id": "tx", "set": {"comment": "a"}},
            {"transaction_id": "tx", "set": {"comment": "b"}},
        ])
    with pytest.raises(MutationValidationError, match="not editable"):
        prepare_transaction_changes(
            financial_db, store,
            [{"transaction_id": "tx", "set": {"changed": 999}}],
        )
```

Also test empty/101-item bounds, non-dict patches, invalid dates, NaN/negative
amounts, missing references, instrument/account mismatch, unpaired `op*` fields,
`deleted=False`, missing raw readiness, 24-hour expiry, 30-day cleanup, mode
`0600`, and idempotent repeated apply requests.

- [ ] **Step 2: Run the new test module and verify RED**

```bash
uv run pytest tests/test_transaction_mutations.py -q
```

Expected: collection FAIL because `transaction_mutations` does not exist.

- [ ] **Step 3: Implement the minimal ledger and validator**

Use these public names and fixed constants:

```python
DEFAULT_MUTATION_PATH = Path("/sync-control/mutation-proposals.db")
MAX_PROPOSAL_ITEMS = 100
PREPARED_TTL_SECONDS = 24 * 60 * 60
TERMINAL_RETENTION_SECONDS = 30 * 24 * 60 * 60

class MutationValidationError(ValueError): ...
class MutationStateError(ValueError): ...

class ProposalStore:
    def create(self, items: list[dict[str, Any]], now: int) -> str: ...
    def get(self, proposal_id: str, now: int) -> dict[str, Any] | None: ...
    def request_apply(self, proposal_id: str, now: int) -> dict[str, Any]: ...
    def claim(self, proposal_id: str | None, now: int) -> dict[str, Any] | None: ...
    def finish(self, proposal_id: str, status: str, item_results: dict[str, str], failure_code: str | None, now: int) -> dict[str, Any]: ...
    def recover_running(self, now: int) -> int: ...
    def close(self) -> None: ...
```

Use `BEGIN IMMEDIATE` for state transitions and a foreign key from items to
proposals. Store canonical compact JSON and render only the changed-field
preview. Validate references with parameterized SQLite queries.

- [ ] **Step 4: Run ledger tests and the full database group**

```bash
uv run pytest tests/test_transaction_mutations.py tests/test_hardened_database.py tests/test_database.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the proposal layer**

```bash
git add src/zenmoney_mcp/transaction_mutations.py tests/test_transaction_mutations.py
git commit -m "feat: add transaction change proposals" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 3: Credentialed mutation executor

**Files:**
- Modify: `src/zenmoney_mcp/hardened_sync.py`
- Modify: `src/zenmoney_mcp/transaction_mutations.py`
- Modify: `tests/test_hardened_sync.py`
- Modify: `tests/test_transaction_mutations.py`

**Interfaces:**
- Produces: `HardenedSyncEngine.push_transactions(transactions: list[dict[str, Any]]) -> dict[str, Any]`
- Produces: `execute_transaction_proposal(db, engine, store, proposal_id, now=None) -> Awaitable[dict[str, Any]]`

- [ ] **Step 1: Write failing API payload and executor tests**

The HTTP-boundary test supplies a complete response and asserts the real request
payload, not the mock itself as the outcome:

```python
@pytest.mark.asyncio
async def test_push_transactions_sends_full_objects_and_applies_response(monkeypatch):
    db = full_db()
    seen = {}
    monkeypatch.setattr(
        "zenmoney_mcp.hardened_sync.httpx.AsyncClient",
        client_returning({"serverTimestamp": 101, "transaction": []}, seen),
    )

    result = await HardenedSyncEngine(db, "token").push_transactions([
        {"id": "tx", "user": 1, "changed": 100, "created": 1,
         "date": "2026-08-25", "income": 0, "outcome": 10,
         "incomeAccount": "cash", "outcomeAccount": "cash",
         "incomeInstrument": 1, "outcomeInstrument": 1, "deleted": False}
    ])

    assert seen["json"]["serverTimestamp"] == 100
    assert seen["json"]["transaction"][0]["id"] == "tx"
    assert result["new_server_timestamp"] == 101
```

Executor tests use a deterministic fake engine whose `sync` mutates the real
test database. Prove: sync-before-write, whole-batch conflict with zero push
calls, successful verification, partial verification as `needs_review`,
transport ambiguity as `needs_review`, and repeated apply returning the terminal
result without another push.

- [ ] **Step 2: Run executor tests and verify RED**

```bash
uv run pytest tests/test_hardened_sync.py tests/test_transaction_mutations.py -q
```

Expected: FAIL because `push_transactions` and `execute_transaction_proposal`
do not exist.

- [ ] **Step 3: Refactor one sanitized diff POST path and implement execution**

Factor the existing HTTP call into a private method used by `sync` and
`push_transactions`. The write method adds `transaction` to the normal diff
request and feeds the response through `apply_diff_data(force_full=False)`.

The executor must:

```python
async def execute_transaction_proposal(db, engine, store, proposal_id, now=None):
    proposal = store.claim(proposal_id, timestamp)
    if proposal is None or proposal["status"] != "running":
        return store.get(proposal_id, timestamp)
    await engine.sync(force_full=False)
    # Compare every expected_changed before constructing any API payload.
    # Revalidate patches, set one changed timestamp, push once, sync once.
    # Finish with applied/conflicted/failed/needs_review and fixed failure codes.
```

Catch upstream HTTP/status/JSON failures only around the write attempt and map
them to `needs_review`; never include exception strings in persisted or MCP
output.

- [ ] **Step 4: Run focused executor and sync tests**

```bash
uv run pytest tests/test_hardened_sync.py tests/test_transaction_mutations.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the executor**

```bash
git add src/zenmoney_mcp/hardened_sync.py src/zenmoney_mcp/transaction_mutations.py tests/test_hardened_sync.py tests/test_transaction_mutations.py
git commit -m "feat: execute confirmed transaction changes" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 4: Local and remote MCP tools

**Files:**
- Modify: `src/zenmoney_mcp/server.py`
- Modify: `src/zenmoney_mcp/http_server.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_remote_http.py`

**Interfaces:**
- Extends: `call_tool(..., mutation_path: str | Path | None = None)`
- Extends: `create_server(..., mutation_path: str | Path | None = None)`
- Extends: `create_app(..., mutation_path: str | Path | None = None)`

- [ ] **Step 1: Write failing MCP contract tests**

Extend the real MCP client test to require all three tools on remote and stdio.
Assert exact annotations: prepare is local-write/non-destructive/closed-world,
get is read-only/non-destructive/closed-world, and apply is
write/destructive/open-world.

Add real dispatch tests:

```python
prepared = await server.call_tool(
    "prepare_transaction_changes",
    {"changes": [{"transaction_id": "tx", "set": {"comment": "fixed"}}]},
    db=full_db,
    remote=True,
    mutation_path=tmp_path / "proposals.db",
)
proposal_id = json.loads(prepared[0].text)["proposal_id"]
queued = await server.call_tool(
    "apply_transaction_changes", {"proposal_id": proposal_id},
    db=full_db, remote=True, mutation_path=tmp_path / "proposals.db",
)
assert json.loads(queued[0].text)["status"] == "pending"
```

For local stdio, replace only the external engine and prove the dispatch awaits
the common executor. Test unknown keys, invalid UUIDs, missing proposals, and
sanitized error results.

- [ ] **Step 2: Run MCP tests and verify RED**

```bash
uv run pytest tests/test_tools.py tests/test_remote_http.py -q
```

Expected: FAIL because the new tools are absent.

- [ ] **Step 3: Register strict schemas and dispatch through the feature module**

Add the three tool descriptors to both surfaces. Derive the default proposal
path from the sync-control directory for remote and from the local cache
directory for stdio. Open and close one `ProposalStore` per request; never keep
financial payloads in global server state.

Remote apply calls `ProposalStore.request_apply`. Local apply calls
`execute_transaction_proposal` with `get_sync_engine()`. All public failures use
fixed codes such as `mutation_not_ready`, `invalid_proposal`,
`proposal_not_found`, and `invalid_transaction_change`.

- [ ] **Step 4: Run MCP tests and server regressions**

```bash
uv run pytest tests/test_tools.py tests/test_remote_http.py tests/test_entrypoint.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the MCP surface**

```bash
git add src/zenmoney_mcp/server.py src/zenmoney_mcp/http_server.py tests/test_tools.py tests/test_remote_http.py
git commit -m "feat: expose confirmed transaction changes" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 5: Remote worker, deployment contract, and operator docs

**Files:**
- Modify: `src/zenmoney_mcp/sync_worker.py`
- Modify: `tests/test_sync_worker.py`
- Modify: `tests/test_remote_deployment.py`
- Modify: `deploy/remote-mcp/README.md`
- Modify: `docs/remote-mcp-threat-model.md`
- Modify: `README.md`

**Interfaces:**
- Extends: `run_worker(..., mutation_step: Callable[[], Awaitable[bool]] | None = None)`
- Produces: `execute_next_mutation() -> Awaitable[bool]`

- [ ] **Step 1: Write failing worker and deployment tests**

Test that the worker polls and executes one pending proposal during its normal
wait loop, resets the scheduled-sync deadline after mutation execution, and
marks leftover `running` proposals `needs_review` on startup. Existing sync
single-flight behavior must remain unchanged.

Extend Compose behavior tests to prove the existing control volume is mounted
read-write in MCP and worker, the MCP service still has no ZenMoney secret or
egress network, and the worker remains the only role with the token.

- [ ] **Step 2: Run worker/deployment tests and verify RED**

```bash
uv run pytest tests/test_sync_worker.py tests/test_remote_deployment.py -q
```

Expected: FAIL because the worker does not process mutation proposals and docs
do not describe the operator gate.

- [ ] **Step 3: Add mutation polling without a second service**

`execute_next_mutation` opens the configured financial database, proposal
store, token, and hardened engine; it claims at most one proposal, executes it,
then closes both databases. `run_worker` calls the mutation step between control
polls. A mutation step returning `True` resets the normal sync deadline.

Do not add a new container, dependency, port, environment variable, or volume.
Use `/sync-control/mutation-proposals.db`, already covered by the mounted named
volume.

- [ ] **Step 4: Document the exact safety boundary and manual gate**

README documents the three tools, two-step examples, full-sync prerequisite,
24-hour/30-day retention, and `needs_review`. The deployment runbook documents
backup sensitivity for the proposal DB and the disposable-transaction live
check. The threat model records proposal previews as financial data and the MCP
server's narrow write access to the control volume.

- [ ] **Step 5: Run worker, deployment, and documentation-adjacent tests**

```bash
uv run pytest tests/test_sync_worker.py tests/test_remote_deployment.py tests/test_remote_http.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit runtime and documentation**

```bash
git add src/zenmoney_mcp/sync_worker.py tests/test_sync_worker.py tests/test_remote_deployment.py deploy/remote-mcp/README.md docs/remote-mcp-threat-model.md README.md
git commit -m "feat: process remote transaction changes" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 6: Final contract verification and delivery

**Files:**
- Modify only if a failing verification exposes a feature defect.

**Interfaces:**
- Consumes all prior task interfaces.
- Produces a verified branch and GitHub pull request against `main`.

- [ ] **Step 1: Run formatting-independent static checks**

```bash
uv run python -m compileall -q src tests
git diff --check origin/main...HEAD
```

Expected: both commands exit 0.

- [ ] **Step 2: Run the complete non-live suite**

```bash
uv run pytest tests/ -q --ignore=tests/test_integration.py
```

Expected: PASS with zero failures.

- [ ] **Step 3: Validate container and Compose contracts**

```bash
docker build -t zenmoney-mcp:transaction-mutations-test .
docker compose -f deploy/remote-mcp/compose.yaml config --quiet
```

Expected: both commands exit 0. No live secret or production Compose file is
read.

- [ ] **Step 4: Review the complete diff against the spec**

```bash
git status --short
git diff --stat origin/main...HEAD
git diff origin/main...HEAD
```

Confirm every spec section has implementation and test evidence, no secret or
generated database is tracked, and the live gate remains explicitly not run.

- [ ] **Step 5: Push the exact feature refspec**

```bash
git fetch origin
git merge-base --is-ancestor origin/main HEAD
git push --dry-run origin HEAD:refs/heads/codex/transaction-mutations
git push -u origin HEAD:refs/heads/codex/transaction-mutations
```

Expected: ancestry check and both pushes exit 0.

- [ ] **Step 6: Create and verify the GitHub pull request**

```bash
gh pr create --repo ekho/zenmoney-mcp --base main --head codex/transaction-mutations --title "feat: add confirmed transaction mutations" --body-file /tmp/zenmoney-transaction-mutations-pr.md
gh pr view --repo ekho/zenmoney-mcp --json url,state,isDraft,baseRefName,headRefName,statusCheckRollup
```

The PR body states the security boundary, tests run, manual live gate not run,
and that merge/deployment are out of scope. Expected: open PR with base `main`
and head `codex/transaction-mutations`.
