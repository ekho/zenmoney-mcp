# Secure Remote MCP Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a private, read-only ChatGPT MCP deployment through OpenAI Secure MCP Tunnel while preserving the local stdio server.

**Architecture:** Migrate the existing explicit MCP registry/dispatcher to the official Python SDK v2 low-level handler API, then construct full stdio and filtered Streamable HTTP servers from it. The HTTP role opens SQLite read-only per request; a separate worker is the only role that syncs with ZenMoney.

**Tech Stack:** Python 3.11+, MCP Python SDK 2.0, Starlette/Uvicorn supplied by MCP, stdlib SQLite/asyncio/logging, httpx, pytest, Docker Compose, official OpenAI tunnel-client v0.0.12.

**Spec:** `docs/superpowers/specs/2026-08-23-remote-mcp-deployment-design.md`

## Global Constraints

- MCP protocol revision: `2026-07-28`; dependency: `mcp>=2.0.0,<3`.
- Keep `zenmoney-mcp` stdio behavior and all current financial semantics.
- Remote tools are cache-only and read-only; exclude `sync_data`, `suggest_category`, and future remote-ineligible tools.
- No public MCP port, reverse proxy, custom OAuth, widget, public submission, Kubernetes, or new heavy dependency.
- Only the sync role receives `ZENMONEY_TOKEN`; only tunnel-client receives the OpenAI runtime key.
- Use official `ghcr.io/openai/tunnel-client:v0.0.12` pinned by verified digest.
- Tests and CI use synthetic SQLite data and no live credentials.
- Every commit uses Conventional Commits and includes `Co-Authored-By: OpenAI Codex <codex@openai.com>`.

---

### Task 1: Migrate the shared MCP registry to SDK v2

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/zenmoney_mcp/server.py`
- Modify: `src/zenmoney_mcp/entrypoint.py`
- Modify: tests that access MCP v1 camel-case attributes
- Test: `tests/test_entrypoint.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_planning_mcp.py`
- Test: `tests/test_decision_mcp.py`

**Interfaces:**
- Produces: `list_tools(remote: bool = False) -> list[Tool]`
- Produces: `call_tool(name: str, arguments: dict[str, Any], *, db: Database | None = None, remote: bool = False) -> list[TextContent]`
- Produces: `list_resources() -> list[Resource]`
- Produces: `read_resource(uri: str, *, db: Database | None = None) -> str`
- Produces: `create_server(*, remote: bool = False) -> mcp.server.Server`

- [ ] **Step 1: Pin the required SDK and update the lock**

Change `pyproject.toml` to `"mcp>=2.0.0,<3"`, run `uv lock --upgrade-package mcp`, and confirm the lock resolves `mcp==2.0.0` and matching `mcp-types`.

- [ ] **Step 2: Write the failing SDK-v2 registry tests**

Update assertions to use SDK v2 snake-case properties (`input_schema`) and add:

```python
def test_server_factory_constructs_local_and_remote_servers():
    assert server.create_server(remote=False).name == "zenmoney-mcp"
    assert server.create_server(remote=True).name == "zenmoney-mcp"
```

Run: `uv run pytest tests/test_entrypoint.py tests/test_tools.py tests/test_planning_mcp.py tests/test_decision_mcp.py -q`

Expected: FAIL on v1 imports/decorators or missing `create_server`.

- [ ] **Step 3: Port descriptors and handlers without duplicating business logic**

Import wire types from `mcp_types`. Keep the existing descriptor functions and dispatch branches. Replace decorator registration with constructor callbacks that wrap results explicitly:

```python
async def _on_list_tools(context, params):
    return ListToolsResult(tools=await list_tools(remote=remote))

async def _on_call_tool(context, params):
    content = await call_tool(
        params.name,
        dict(params.arguments or {}),
        remote=remote,
    )
    return CallToolResult(content=content)
```

Build `Server(name="zenmoney-mcp", version=__version__, on_list_tools=..., on_call_tool=..., on_list_resources=..., on_read_resource=...)`. Keep `main()` running this server over stdio.

- [ ] **Step 4: Preserve hardening without runtime monkey-patching MCP registration**

Make `server.py` directly use `HardenedDatabase`, `HardenedSyncEngine`, and corrected analytics. Reduce `entrypoint.py` to the stable stdio entrypoint; retain `harden_tool_schemas()` only if descriptor construction still needs it.

- [ ] **Step 5: Run the focused and complete Python suites**

Run:

```bash
uv run pytest tests/test_entrypoint.py tests/test_tools.py tests/test_planning_mcp.py tests/test_decision_mcp.py -q
uv run pytest tests/ -q --ignore=tests/test_integration.py
```

Expected: 0 failures.

- [ ] **Step 6: Commit**

Commit: `refactor: migrate shared MCP registry to SDK v2`

---

### Task 2: Add configurable read-only SQLite access

**Files:**
- Modify: `src/zenmoney_mcp/database.py`
- Modify: `src/zenmoney_mcp/hardened_database.py`
- Modify: `src/zenmoney_mcp/server.py`
- Test: `tests/test_remote_database.py`

**Interfaces:**
- Produces: `Database(db_path: str | Path | None = None, *, read_only: bool = False)`
- Produces: `get_database_path() -> Path`
- Produces: `open_remote_db() -> HardenedDatabase`
- Produces: `HardenedDatabase.check_ready() -> bool`

- [ ] **Step 1: Write failing read-only lifecycle tests**

Create tests that assert read-only mode can query an initialized file, rejects writes, does not create a missing file, reports malformed/uninitialized files as not ready, and follows an atomic `os.replace()` after close/reopen:

```python
first = HardenedDatabase(path, read_only=True)
assert first.connect().execute("SELECT value FROM sync_meta WHERE key='snapshot'").fetchone()[0] == "A"
first.close()
os.replace(replacement, path)
second = HardenedDatabase(path, read_only=True)
assert second.connect().execute("SELECT value FROM sync_meta WHERE key='snapshot'").fetchone()[0] == "B"
```

Run: `uv run pytest tests/test_remote_database.py -q`

Expected: FAIL because `read_only` and readiness do not exist.

- [ ] **Step 2: Implement SQLite URI read-only mode**

For file-backed read-only connections use `sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True, check_same_thread=False)`. Skip file creation, chmod, WAL changes, and schema initialization. `check_ready()` runs `PRAGMA quick_check`, verifies the expected `sync_meta` table, catches `sqlite3.Error`, and returns only a boolean.

- [ ] **Step 3: Make the configured path authoritative**

`get_database_path()` returns `Path(os.environ["ZENMONEY_DB_PATH"])` when set, otherwise `~/.cache/zenmoney-mcp/zenmoney.db`. Local `get_db()` creates/migrates it. Remote `open_remote_db()` requires it to exist and opens read-only.

- [ ] **Step 4: Run database and full tests**

Run:

```bash
uv run pytest tests/test_remote_database.py tests/test_database.py tests/test_hardened_database.py -q
uv run pytest tests/ -q --ignore=tests/test_integration.py
```

Expected: 0 failures.

- [ ] **Step 5: Commit**

Commit: `feat: add read-only SQLite snapshot access`

---

### Task 3: Add filtered Streamable HTTP runtime and health endpoints

**Files:**
- Create: `src/zenmoney_mcp/http_server.py`
- Modify: `src/zenmoney_mcp/server.py`
- Modify: `pyproject.toml`
- Test: `tests/test_remote_http.py`

**Interfaces:**
- Produces: `REMOTE_EXCLUDED_TOOLS = frozenset({"sync_data", "suggest_category"})`
- Produces: `create_app(db_path: str | Path | None = None) -> Starlette`
- Produces CLI: `zenmoney-mcp-http`

- [ ] **Step 1: Write failing remote surface tests**

Use the official SDK v2 client against the ASGI app and assert initialization, resource listing, an analytical call, annotations, and exclusions:

```python
names = {tool.name for tool in await client.list_tools()}
assert "get_net_worth" in names
assert not ({"sync_data", "suggest_category"} & names)
assert all(tool.annotations.read_only_hint is True for tool in tools)
with pytest.raises(MCPError):
    await client.call_tool("sync_data", {})
```

Also assert `/healthz` is 200, initialized DB `/readyz` is 200, and missing/malformed DB `/readyz` is 503 with fixed non-sensitive JSON.

Run: `uv run pytest tests/test_remote_http.py -q`

Expected: FAIL because the HTTP runtime does not exist.

- [ ] **Step 2: Filter and annotate remote descriptors**

`list_tools(remote=True)` excludes `REMOTE_EXCLUDED_TOOLS` and returns descriptor copies with `ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)`. `call_tool(..., remote=True)` rejects excluded or undiscovered names before opening a database.

- [ ] **Step 3: Open and close SQLite per remote request**

When `remote=True`, wrap every tool/resource dispatch in `db = open_remote_db()` / `finally: db.close()`. Passing an explicit `db` in tests bypasses environment lookup but remains caller-owned.

- [ ] **Step 4: Build the Starlette app**

Create the official MCP app with `streamable_http_path="/mcp"`, `stateless_http=True`, and `json_response=True`. Add exact `/healthz` and `/readyz` routes and carry the MCP lifespan into the outer Starlette application. Run Uvicorn with access logs disabled and host/port from `ZENMONEY_HTTP_HOST`/`ZENMONEY_HTTP_PORT`.

- [ ] **Step 5: Verify replacement without process restart**

The integration test starts one app, calls `get_sync_status` against snapshot A, atomically replaces the database with snapshot B, and makes a second MCP request through the same client/app. Assert the second result contains B's timestamp.

- [ ] **Step 6: Run focused and complete tests**

Run:

```bash
uv run pytest tests/test_remote_http.py -q
uv run pytest tests/ -q --ignore=tests/test_integration.py
```

Expected: 0 failures.

- [ ] **Step 7: Commit**

Commit: `feat: add read-only Streamable HTTP server`

---

### Task 4: Add sync worker, token files, and safe backup

**Files:**
- Create: `src/zenmoney_mcp/sync_worker.py`
- Create: `src/zenmoney_mcp/backup.py`
- Modify: `pyproject.toml`
- Test: `tests/test_sync_worker.py`
- Test: `tests/test_backup.py`

**Interfaces:**
- Produces: `read_secret(name: str) -> str`
- Produces: `parse_interval(value: str | None) -> int`
- Produces: `sync_once() -> dict[str, Any]`
- Produces: `run_worker(sync: Callable[[], Awaitable[Any]], interval: int, stop: asyncio.Event) -> None`
- Produces CLIs: `zenmoney-sync-once`, `zenmoney-sync-worker`, `zenmoney-db-backup`

- [ ] **Step 1: Write failing worker and secret tests**

Test file-first secret precedence, blank/missing rejection, default `900`, accepted `0`, negative/non-integer rejection, immediate sync, periodic wait, stop during wait, and failure followed by one interval before retry. Capture logs and assert neither a sentinel token nor exception message/body appears.

- [ ] **Step 2: Implement minimal worker**

Use stdlib `asyncio.Event`, `loop.add_signal_handler(SIGTERM/SIGINT, stop.set)`, and `asyncio.wait_for(stop.wait(), timeout=interval)`. Emit one-line JSON through `json.dumps` with fixed keys. `interval=0` performs one sync and returns.

- [ ] **Step 3: Write failing online-backup tests**

Create a WAL-backed source, run the backup function while the source is open, and assert the destination passes `quick_check` and contains committed rows. Reject destination overwrite unless `--force` is passed.

- [ ] **Step 4: Implement backup with SQLite's online API**

Open source read-only, destination normally, call `source.connect().backup(destination.connect())`, commit, close, chmod destination `0600`, and print only path/success metadata.

- [ ] **Step 5: Run focused and complete tests**

Run:

```bash
uv run pytest tests/test_sync_worker.py tests/test_backup.py tests/test_hardened_sync.py -q
uv run pytest tests/ -q --ignore=tests/test_integration.py
```

Expected: 0 failures.

- [ ] **Step 6: Commit**

Commit: `feat: add background ZenMoney sync worker`

---

### Task 5: Add hardened Docker and Secure MCP Tunnel deployment

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `deploy/remote-mcp/compose.yaml`
- Create: `deploy/remote-mcp/.env.example`
- Create: `deploy/remote-mcp/secrets/.gitignore`
- Create: `tests/test_remote_deployment.py`

**Interfaces:**
- Application image commands: `zenmoney-mcp-http`, `zenmoney-sync-worker`, `zenmoney-sync-once`
- Compose services: `zenmoney-mcp`, `zenmoney-sync`, `tunnel-client`
- Networks: `mcp_internal`, `egress`
- Volume: `zenmoney-data`

- [ ] **Step 1: Verify and record the official image digest**

Run `docker buildx imagetools inspect ghcr.io/openai/tunnel-client:v0.0.12` and record the manifest-list digest in Compose and the deployment README. Do not use a platform-specific child digest.

- [ ] **Step 2: Write failing static deployment tests**

Parse `docker compose -f deploy/remote-mcp/compose.yaml config --format json` and assert:

```python
assert "ports" not in services["zenmoney-mcp"]
assert services["zenmoney-mcp"]["volumes"][0]["read_only"] is True
assert "ZENMONEY_TOKEN" not in services["zenmoney-mcp"].get("environment", {})
assert "CONTROL_PLANE_API_KEY" not in services["zenmoney-mcp"].get("environment", {})
assert "ZENMONEY_TOKEN" not in services["tunnel-client"].get("environment", {})
assert networks["mcp_internal"]["internal"] is True
```

Run: `uv run pytest tests/test_remote_deployment.py -q`

Expected: FAIL because deployment files do not exist.

- [ ] **Step 3: Build one non-root application image**

Use a pinned Python 3.12 slim base, install with `uv sync --frozen --no-dev`, copy only runtime files, create an unprivileged user, set `PYTHONDONTWRITEBYTECODE=1`, and add the HTTP healthcheck. Do not bake credentials or a database into the image.

- [ ] **Step 4: Add the three-role Compose topology**

Use secrets mounted under `/run/secrets`, named volume `/data`, `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, tmpfs `/tmp`, `restart: unless-stopped`, and no host port publication. Configure tunnel-client with the official HTTP server URL and its documented file-key option.

- [ ] **Step 5: Validate and smoke the containers**

Run:

```bash
docker build -t zenmoney-mcp:remote-test .
docker compose -f deploy/remote-mcp/compose.yaml config -q
uv run pytest tests/test_remote_deployment.py -q
```

Start `zenmoney-mcp` and `zenmoney-sync` with synthetic secret/DB fixtures and a test image override; verify their configured user, health, and volume modes with `docker inspect`. Do not start tunnel-client without its runtime credentials.

- [ ] **Step 6: Commit**

Commit: `feat: add secure tunnel Docker deployment`

---

### Task 6: Add operations, threat model, README, and CI

**Files:**
- Create: `deploy/remote-mcp/README.md`
- Create: `docs/remote-mcp-threat-model.md`
- Modify: `README.md`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Documents exact setup, association, secret, start, verify, backup, restore, rollback, and manual ChatGPT steps.
- CI jobs: Python, Docker/Compose, remote MCP smoke.

- [ ] **Step 1: Write the deployment runbook from verified commands**

Document Platform tunnel settings, organization/workspace association, `chmod 600` secret creation, Compose commands, tunnel doctor, internal health checks, read-only tool scan, safe backup/restore, `down` versus destructive `down -v`, single-replica limit, and egress firewall responsibility. Mark tunnel and ChatGPT checks manual unless actually executed.

- [ ] **Step 2: Write the compact threat model**

Cover the four specified assets, seven trust boundaries, and each required threat with its concrete mitigation. Keep it operational and under roughly two pages.

- [ ] **Step 3: Update the root README**

Describe local stdio and remote Streamable HTTP + Secure MCP Tunnel modes, link the runbook/threat model, and state which tools are excluded remotely.

- [ ] **Step 4: Extend CI**

Keep the Python 3.11–3.13 matrix. Add Docker build, Compose config/static security tests, and local HTTP smoke without secrets. Do not start tunnel-client in CI.

- [ ] **Step 5: Run documentation/config checks and full verification**

Run:

```bash
python -m compileall -q src tests
uv run pytest -q --ignore=tests/test_integration.py
git diff --check
docker build -t zenmoney-mcp:remote-test .
docker compose -f deploy/remote-mcp/compose.yaml config -q
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

Commit: `docs: add remote MCP operations guide`

---

### Task 7: Audit, publish, and observe the draft PR

**Files:**
- Review all branch changes; no new implementation file is expected.

**Interfaces:**
- Produces remote branch `origin/feature/remote-mcp-deployment`
- Produces draft PR `feat: add secure remote MCP deployment` targeting `main`

- [ ] **Step 1: Re-run the complete acceptance commands fresh**

Run the exact compile, full non-live test, HTTP replacement test, Docker build, Compose config, static secret/network tests, `git diff --check`, `git status`, and secret/SQLite tracked-file scans. Record exact counts and exit status.

- [ ] **Step 2: Verify every Definition of Done item**

Map the 27 requested items to command evidence or `manual/not run`. Do not infer tunnel or ChatGPT UI success from local tests.

- [ ] **Step 3: Verify publication destination before push**

Run:

```bash
git branch --show-current
git status --short --branch
git remote get-url --push origin
git push --dry-run origin HEAD:refs/heads/feature/remote-mcp-deployment
```

Expected destination: `git@github.com:ekho/zenmoney-mcp.git`, exact feature ref.

- [ ] **Step 4: Push explicitly and create the draft PR**

Run:

```bash
git push origin HEAD:refs/heads/feature/remote-mcp-deployment
gh pr create --repo ekho/zenmoney-mcp --base main --head feature/remote-mcp-deployment --draft --title "feat: add secure remote MCP deployment" --body-file /tmp/zenmoney-remote-mcp-pr-body.md
```

Create `/tmp/zenmoney-remote-mcp-pr-body.md` with Summary, Security, Testing, Deployment, and Manual step sections. Substitute only the measured test count, image digest, and CI state into this exact structure before passing it to `gh pr create`:

```markdown
## Summary
- Streamable HTTP MCP transport
- read-only remote tool surface
- background ZenMoney sync worker
- persistent SQLite cache
- OpenAI Secure MCP Tunnel deployment
- Docker hardening

## Security
- no public MCP ingress
- MCP server has no ZenMoney token or OpenAI runtime key
- tunnel-client has no ZenMoney token
- sync worker is the only ZenMoney API credential holder
- financial data and tool payloads are not logged

## Testing
- Replace this instruction with one bullet per Step 1 command containing its literal command, exit status, and measured test count.

## Deployment
ChatGPT reaches the private `/mcp` endpoint through OpenAI Secure MCP Tunnel; tunnel-client and the server share only an internal Docker network.

## Manual step
- create and associate the OpenAI tunnel, then provide its runtime credentials
- run tunnel-client doctor
- scan tools and call one read-only analytic from ChatGPT
```

- [ ] **Step 5: Observe CI without merging**

Use `gh pr checks --watch` or bounded GitHub status checks. Report each job's actual conclusion and leave the PR draft and unmerged.
