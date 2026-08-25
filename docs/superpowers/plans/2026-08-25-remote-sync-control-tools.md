# Remote Sync Control Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add asynchronous remote `force_sync` and `get_sync_status` tools while keeping the ZenMoney token and financial-cache writes confined to the sync worker.

**Architecture:** A small JSON state file on a dedicated shared volume is protected by a stable `fcntl.flock` file and atomically replaced. The remote MCP records one pending request; the existing worker polls, executes it, and records a sanitized terminal state.

**Tech Stack:** Python 3.11 stdlib (`fcntl`, `json`, `os`, `tempfile`, `time`, `uuid`), MCP Python SDK 2, Docker Compose, pytest.

**Spec:** `docs/superpowers/specs/2026-08-25-remote-sync-control-tools-design.md`

## Global Constraints

- Keep `ZENMONEY_TOKEN` and `ZENMONEY_TOKEN_FILE` out of the MCP container.
- Keep the financial `/data` mount read-only in MCP.
- Do not add a database, broker, HTTP control service, or dependency.
- Remote `force_sync` is asynchronous and single-flight; full sync never blocks the tool call.
- State and logs contain no credentials, API bodies, financial data, paths, or exception text.
- Preserve local `sync_data(force_full=...)` and `zenmoney://sync-status` behavior.
- Do not commit, push, publish, merge, or deploy without separate authorization.

---

### Task 1: Locked JSON sync-control state

**Files:**
- Create: `src/zenmoney_mcp/sync_control.py`
- Create: `tests/test_sync_control.py`

**Interfaces:**
- Produces: `InvalidSyncState`, `read_sync_state(path: Path) -> dict[str, Any]`, `request_sync(path: Path, force_full: bool) -> dict[str, Any]`, `claim_sync_request(path: Path) -> dict[str, Any] | None`, and `finish_sync_request(path: Path, request_id: str, succeeded: bool) -> dict[str, Any]`.
- State keys: `state`, `request_id`, `force_full`, `requested_at`, `started_at`, `finished_at`, `failure_code`.

- [ ] **Step 1: Write failing state-transition tests**

```python
def test_request_sync_is_single_flight(tmp_path):
    path = tmp_path / "sync-state.json"
    first = request_sync(path, force_full=True)
    second = request_sync(path, force_full=False)
    assert first["status"] == "accepted"
    assert second["status"] == "already_running"
    assert second["request_id"] == first["request_id"]
    assert read_sync_state(path)["force_full"] is True

def test_claim_and_finish_sync_request(tmp_path):
    path = tmp_path / "sync-state.json"
    requested = request_sync(path, force_full=False)
    claimed = claim_sync_request(path)
    assert claimed["state"] == "running"
    completed = finish_sync_request(path, requested["request_id"], succeeded=True)
    assert completed["state"] == "completed"
    assert completed["failure_code"] is None
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `uv run python -m pytest tests/test_sync_control.py -v`

Expected: collection fails because `zenmoney_mcp.sync_control` does not exist.

- [ ] **Step 3: Add the minimal locked state implementation**

Create `sync_control.py` with a 4 KiB read limit, exact-key/type/state validation, an idle result for an absent file, a stable sibling `.lock` file, exclusive `fcntl.flock`, and same-directory temporary writes published through `os.replace`. `request_sync` must preserve an existing pending/running request; `claim_sync_request` must reclaim both pending and restart-leftover running requests; `finish_sync_request` must reject a mismatched request ID and write only `completed` or `failed/sync_failed`.

- [ ] **Step 4: Add malformed-state and real concurrency tests**

```python
def test_invalid_state_is_never_claimed(tmp_path):
    path = tmp_path / "sync-state.json"
    path.write_text('{"state":"running","token":"secret"}')
    with pytest.raises(InvalidSyncState):
        claim_sync_request(path)

def test_concurrent_requests_share_one_request_id(tmp_path):
    path = tmp_path / "sync-state.json"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda full: request_sync(path, full), (True, False)))
    assert {result["status"] for result in results} == {"accepted", "already_running"}
    assert len({result["request_id"] for result in results}) == 1
```

- [ ] **Step 5: Verify GREEN**

Run: `uv run python -m pytest tests/test_sync_control.py -v`

Expected: all sync-control tests pass without warnings.

- [ ] **Step 6: Record a local checkpoint**

Run: `git diff --check && git status --short`

Expected: only the approved spec, plan, control module, and control tests are changed; no commit is created.

---

### Task 2: Worker-triggered incremental and full synchronization

**Files:**
- Modify: `src/zenmoney_mcp/sync_worker.py`
- Modify: `tests/test_sync_worker.py`

**Interfaces:**
- Consumes: the Task 1 control functions.
- Changes: `sync_once(force_full: bool = False) -> dict[str, Any]` and `run_worker(sync: Callable[[bool], Awaitable[Any]], interval: int, stop: asyncio.Event, control_path: Path = DEFAULT_CONTROL_PATH) -> None`.
- Produces: scheduled calls with `force_full=False`; requested calls with the stored mode and persisted terminal status.

- [ ] **Step 1: Write failing worker tests**

```python
@pytest.mark.asyncio
async def test_worker_claims_full_request_before_scheduled_sync(tmp_path):
    control = tmp_path / "sync-state.json"
    requested = request_sync(control, force_full=True)
    calls = []
    async def sync(force_full):
        calls.append(force_full)
    await run_worker(sync, 0, asyncio.Event(), control)
    assert calls == [True]
    state = read_sync_state(control)
    assert state["request_id"] == requested["request_id"]
    assert state["state"] == "completed"

@pytest.mark.asyncio
async def test_worker_records_requested_sync_failure(tmp_path):
    control = tmp_path / "sync-state.json"
    request_sync(control, force_full=False)
    async def sync(force_full):
        raise RuntimeError("sensitive response")
    await run_worker(sync, 0, asyncio.Event(), control)
    assert read_sync_state(control)["failure_code"] == "sync_failed"
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run python -m pytest tests/test_sync_worker.py -v`

Expected: new tests fail because the worker does not accept a control path or mode.

- [ ] **Step 3: Pass the mode through `sync_once`**

Change the engine call to `HardenedSyncEngine(...).sync(force_full=force_full)` and update its focused test to assert both `False` and `True` reach the engine without exposing the token.

- [ ] **Step 4: Integrate control polling into the existing worker loop**

At startup, claim a pending/restart-leftover request before the scheduled sync. During the interval wait, check once per `CONTROL_POLL_INTERVAL = 1.0`; process a request immediately and reset the interval deadline after it finishes. Keep `interval=0` as one attempt. Catch sync exceptions with the existing fixed log record and always persist requested success/failure.

- [ ] **Step 5: Update existing worker fixtures to accept the boolean mode**

Replace zero-argument test sync callables with `async def sync(force_full): ...` and assert scheduled calls receive `False`. Do not weaken interval, shutdown, or sanitized-log assertions.

- [ ] **Step 6: Verify GREEN**

Run: `uv run python -m pytest tests/test_sync_worker.py tests/test_sync_control.py -v`

Expected: both focused files pass without secret or exception text in logs.

- [ ] **Step 7: Record a local checkpoint**

Run: `git diff --check && git status --short`

Expected: worker changes are present and no commit is created.

---

### Task 3: Remote MCP tools and protocol behavior

**Files:**
- Modify: `src/zenmoney_mcp/server.py`
- Modify: `src/zenmoney_mcp/http_server.py`
- Modify: `tests/test_remote_http.py`
- Modify: `tests/test_entrypoint.py`

**Interfaces:**
- Consumes: `request_sync`, `read_sync_state`, `InvalidSyncState`.
- Produces: remote-only `force_sync(force_full: bool = False)` and `get_sync_status()` descriptors; `create_app(db_path=None, control_path=None)` test seam.

- [ ] **Step 1: Write failing remote discovery and dispatch tests**

```python
@pytest.mark.asyncio
async def test_remote_sync_tools_have_truthful_annotations(tmp_path):
    app = create_app(tmp_path / "missing.db", tmp_path / "sync-state.json")
    async with _mcp_client(app) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        result = await client.call_tool("force_sync", {"force_full": True})
    assert tools["force_sync"].annotations.read_only_hint is False
    assert tools["force_sync"].annotations.open_world_hint is True
    assert tools["get_sync_status"].annotations.read_only_hint is True
    assert json.loads(result.content[0].text)["status"] == "accepted"
```

Also assert local discovery does not add either remote-only tool and that direct `sync_data` remains rejected remotely.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run python -m pytest tests/test_remote_http.py tests/test_entrypoint.py -v`

Expected: remote discovery lacks both tools and `create_app` rejects the control-path argument.

- [ ] **Step 3: Add remote-only descriptors and per-tool annotations**

Extend `list_tools(remote=True)` with strict schemas (`additionalProperties: false`). Keep existing analytical tools read-only/closed-world, annotate `force_sync` as non-read-only/non-destructive/open-world, and leave `get_sync_status` read-only/non-destructive/closed-world.

- [ ] **Step 4: Dispatch control tools before opening the financial database**

`force_sync` must work when the snapshot does not yet exist. It maps the internal boolean to public `mode: incremental|full` and returns immediately. `get_sync_status` maps invalid control JSON to fixed `failed/invalid_sync_state`, reads existing cache status when a valid database exists, and otherwise returns `last_sync_time: null`, `staleness: never_synced`, and an empty `cache_stats`.

- [ ] **Step 5: Thread the control path through the remote server factory**

Add optional `control_path` parameters to `create_app`, `create_server`, and `call_tool`; production defaults to `/sync-control/sync-state.json`. Do not add the tools to local stdio discovery.

- [ ] **Step 6: Verify GREEN**

Run: `uv run python -m pytest tests/test_remote_http.py tests/test_entrypoint.py tests/test_sync_control.py -v`

Expected: protocol calls, annotations, missing-snapshot behavior, and local compatibility pass.

- [ ] **Step 7: Record a local checkpoint**

Run: `git diff --check && git status --short`

Expected: server/runtime changes are present and no commit is created.

---

### Task 4: Compose boundary, documentation, and full verification

**Files:**
- Modify: `deploy/remote-mcp/compose.yaml`
- Modify: `tests/test_remote_deployment.py`
- Modify: `README.md`
- Modify: `deploy/remote-mcp/README.md`
- Modify: `docs/remote-mcp-threat-model.md`

**Interfaces:**
- Consumes: fixed production control path `/sync-control/sync-state.json`.
- Produces: a `zenmoney-sync-control` named volume mounted read-write only in `zenmoney-mcp` and `zenmoney-sync`.

- [ ] **Step 1: Write the failing Compose boundary assertions**

```python
mcp_mounts = {mount["target"]: mount for mount in services["zenmoney-mcp"]["volumes"]}
sync_mounts = {mount["target"]: mount for mount in services["zenmoney-sync"]["volumes"]}
assert mcp_mounts["/data"]["read_only"] is True
assert mcp_mounts["/sync-control"]["read_only"] is False
assert sync_mounts["/sync-control"]["read_only"] is False
assert "/sync-control" not in {
    mount["target"] for mount in services["tunnel-client"].get("volumes", [])
}
```

- [ ] **Step 2: Run deployment tests and verify RED**

Run: `uv run python -m pytest tests/test_remote_deployment.py -v`

Expected: `/sync-control` mount assertions fail.

- [ ] **Step 3: Add the named volume without changing credential mounts**

Mount `zenmoney-sync-control:/sync-control` in MCP and worker, declare the volume, and keep all existing networks, users, read-only roots, `/data` permissions, and secrets unchanged.

- [ ] **Step 4: Update human-facing contracts**

README: replace the “remote registry is read-only” claim with the bounded sync-control exception and list both tools. Runbook: document asynchronous full sync, status polling, control-state recovery, and manual ChatGPT checks with truthful annotations. Threat model: add the control volume crossing, strict state validation, single-flight behavior, and the fact that remote MCP can request but cannot itself perform synchronization.

- [ ] **Step 5: Run focused deployment verification**

Run: `uv run python -m pytest tests/test_remote_deployment.py tests/test_remote_http.py tests/test_sync_worker.py tests/test_sync_control.py -v`

Expected: all focused tests pass.

- [ ] **Step 6: Run full non-live verification**

Run: `uv run python -m compileall -q src tests`

Run: `uv run python -m pytest tests/ -v --ignore=tests/test_integration.py`

Run: `docker compose -f deploy/remote-mcp/compose.yaml config --quiet`

Expected: every command exits 0 with no secret-dependent or live ZenMoney call.

- [ ] **Step 7: Review the final diff**

Run: `git diff --check && git status --short && git diff --stat`

Expected: only the approved feature, its tests, and documentation are changed. Leave all changes uncommitted.
