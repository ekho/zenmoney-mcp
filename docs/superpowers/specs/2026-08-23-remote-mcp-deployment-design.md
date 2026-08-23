# Remote MCP Deployment Design

**Date:** 2026-08-23
**Status:** Approved
**Base:** `origin/main` at `263eee235ada9139795480a8f0e25563ceb0f371`

## Goal

Expose ZenMoney's cached, read-only analytics to a private ChatGPT developer-mode app through OpenAI Secure MCP Tunnel, without a public MCP endpoint and without giving the MCP server ZenMoney or OpenAI credentials.

## Current-source decisions

- Use MCP revision `2026-07-28` through the official Python SDK v2. The repository's existing `mcp<2` constraint cannot serve that revision.
- Use Streamable HTTP at `/mcp`; do not add legacy SSE.
- Use OpenAI Secure MCP Tunnel for private developer-mode access. The tunnel is the connection trust boundary, so this deployment does not add an OAuth provider.
- Pin the current stable official tunnel client, `ghcr.io/openai/tunnel-client:v0.0.12`, by multi-architecture image digest verified during implementation.
- Keep the app `tool-only`. Do not add a widget, Apps directory submission metadata, or generic `search`/`fetch` facades: the existing bounded financial tools are the user-facing contract, not a document corpus.

Authoritative references:

- <https://developers.openai.com/api/docs/guides/secure-mcp-tunnels>
- <https://modelcontextprotocol.io/specification/2026-07-28>
- <https://py.sdk.modelcontextprotocol.io/migration/>
- <https://github.com/openai/tunnel-client/releases/tag/v0.0.12>

## Architecture

The application keeps one tool/resource registry and one dispatcher. Two official MCP transports select different views over that registry:

```text
local client -> stdio server -> full registry -> local SQLite / ZenMoney sync

ChatGPT -> OpenAI tunnel service <- outbound HTTPS <- tunnel-client
                                                   |
                                                   v
                                      private Streamable HTTP /mcp
                                                   |
                                           remote read-only registry
                                                   |
                                          read-only SQLite snapshot

ZenMoney API <- outbound HTTPS <- sync worker -> read-write SQLite snapshot
```

The stdio surface remains backward compatible. The HTTP surface excludes `sync_data`, `suggest_category`, and any future tool marked as remote-ineligible. Both surfaces reuse the same descriptors and call implementation; filtering happens only at server construction and dispatch authorization.

## MCP SDK migration

Migrate the low-level server from MCP Python SDK v1 decorators to the v2 constructor handler API. Keep the existing explicit JSON schemas and dispatcher instead of rewriting every tool as a new decorated function. This is the smallest migration that preserves current behavior and gives both transports the current protocol implementation.

The dependency becomes `mcp>=2.0.0,<3`; `uv.lock` is regenerated. Existing direct `httpx` use remains because it is the ZenMoney API client, independent of the SDK's internal switch to `httpx2`.

## Runtime roles

### Local stdio server

`zenmoney-mcp` continues to start stdio. It may read `ZENMONEY_TOKEN`, expose `sync_data` and `suggest_category`, create/migrate the default cache, and preserve the current local workflows.

### Remote MCP server

`zenmoney-mcp-http` starts one Streamable HTTP server on an internal container port. It exposes `/mcp`, `/healthz`, and `/readyz` only inside Docker networks. Its configuration is `ZENMONEY_DB_PATH` plus bind host/port; it rejects `sync_data`, `suggest_category`, and unknown tools even if called directly instead of discovered first.

Every remote tool has truthful annotations:

- `readOnlyHint=true`
- `destructiveHint=false`
- `openWorldHint=false`

The container receives neither `ZENMONEY_TOKEN` nor `CONTROL_PLANE_API_KEY`.

### Sync worker

`zenmoney-sync-once` performs one hardened Phase 1 sync and exits. `zenmoney-sync-worker` performs an immediate sync, then repeats after `ZENMONEY_SYNC_INTERVAL_SECONDS` (default `900`). `0` means one initial sync with no periodic retry, which is the useful operational meaning for a disabled loop.

The worker supports `ZENMONEY_TOKEN_FILE` first and the existing environment variable second. Errors are logged as exception class plus a fixed message; response bodies, tokens, transactions, balances, and account names are never logged. A failed sync leaves the previous snapshot readable and waits until the next configured cycle. SIGTERM stops the wait cleanly.

## SQLite lifecycle

`ZENMONEY_DB_PATH` overrides the existing user-cache default.

Add explicit read-only database mode using SQLite URI `mode=ro`. Read-only mode does not create the file, change permissions, enable WAL, initialize schema, or silently replace a malformed cache.

The remote dispatcher opens a fresh read-only `HardenedDatabase` for each tool or resource request and closes it afterward. This avoids retaining a descriptor to an inode replaced by a full atomic sync. `/readyz` uses the same short-lived connection and verifies both `PRAGMA quick_check` and the initialized schema without returning cache contents.

The sync worker owns the long-lived read-write database connection and the existing Phase 1 atomic/rollback behavior.

## HTTP and logging

The official SDK owns `/mcp` protocol parsing and response handling with stateless HTTP and JSON responses. A small Starlette wrapper supplies health routes and enters the MCP app lifespan.

Application logs are JSON objects containing only event name, duration, success/failure, exception class, and non-sensitive entity counts. Access logs and raw MCP request logging are disabled. Unexpected exceptions return fixed external text and keep details out of stdout/stderr.

`/healthz` reports only liveness. `/readyz` returns ready/not-ready and an HTTP status; neither includes financial data or raw SQLite errors.

## Container deployment

One non-root Python image runs either the HTTP server or sync worker. It uses a pinned Python slim base, installs from the lockfile, has a read-only root filesystem, drops all Linux capabilities, enables `no-new-privileges`, and uses tmpfs only where runtime writes require it.

Compose defines:

- `zenmoney-mcp`: SQLite volume read-only, only `mcp_internal`, `expose` but no `ports`;
- `zenmoney-sync`: SQLite volume read-write, only the normal egress network, only ZenMoney credentials;
- `tunnel-client`: `mcp_internal` plus egress, only tunnel ID/runtime key, private MCP URL `http://zenmoney-mcp:<port>/mcp`;
- `mcp_internal`: `internal: true`;
- `egress`: normal bridge network.

Docker Compose cannot enforce destination-domain allowlists by itself. The design provides network separation and outbound-only topology; restricting egress specifically to `api.openai.com:443` and `api.zenmoney.ru:443` remains a host/firewall responsibility documented in the runbook.

Secrets are file-mounted Compose secrets. `.env.example` contains identifiers and placeholders only. No secret value or SQLite file is committed.

## Operations

The deployment runbook covers prerequisites, Platform tunnel creation/association, secret-file permissions, start/stop/status/logs, one-shot sync, tunnel doctor, internal health checks, ChatGPT tool scan, backup, restore, and rollback.

Backup uses SQLite's online backup API through an application CLI, not `cp` of a live WAL database. Restore is offline: stop application roles, replace the volume database from a validated backup, then restart. `docker compose down` preserves data; `down -v` is explicitly destructive.

This is a single-user, single-replica SQLite deployment. No horizontal scaling, reverse proxy, public HTTPS fallback, Prometheus stack, Kubernetes, custom OAuth, or widget is included.

## Verification

Tests are synthetic and use no live tokens. They cover:

- unchanged stdio discovery and calls;
- Streamable HTTP initialization, tool/resource discovery, and one analytical call through the official client;
- remote annotations and exclusion/direct rejection of sync/API-dependent tools;
- empty, populated, stale, missing, malformed, restarted, and atomically replaced caches;
- one-shot/periodic worker behavior, interval validation, graceful shutdown, and failure preservation;
- token-file handling and sanitized failures;
- Compose service environments, secret separation, read-only volume, internal network, and absence of published MCP ports;
- Docker image build and container smoke tests without production credentials.

CI runs compile/tests, Docker build, Compose validation, and remote MCP smoke. Live `tunnel-client doctor`, ChatGPT Scan Tools, and a ChatGPT analytics call remain explicitly manual unless credentials and UI access are available.

## Delivery

Work is committed logically on `feature/remote-mcp-deployment`, pushed explicitly to `origin`, and published as a draft PR titled `feat: add secure remote MCP deployment` against `main`. The PR is not merged.
