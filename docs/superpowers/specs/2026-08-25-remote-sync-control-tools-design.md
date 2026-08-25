# Remote Sync Control Tools Design

**Date:** 2026-08-25

**Status:** Approved

**Base:** `d361b77fa9b0d6171f2a9e63004006f0b938483c`

## Goal

Let the private remote MCP request an immediate incremental or full ZenMoney
synchronization and report its progress, without giving the MCP container the
ZenMoney token or write access to the financial snapshot.

## Existing boundaries

The local stdio surface keeps `sync_data(force_full=...)`. The remote MCP
currently excludes that tool, mounts `/data` read-only, and receives no
ZenMoney credential. A separate worker owns both the credential and writes to
the snapshot. This design preserves those ownership boundaries.

## Tool contract

The remote surface adds two tools.

`force_sync` accepts one optional boolean, `force_full`, defaulting to `false`.
It records a request and returns immediately:

```json
{
  "status": "accepted",
  "request_id": "UUID",
  "mode": "incremental",
  "requested_at": 1787600000
}
```

If a request is already `pending` or `running`, the tool does not enqueue
another one. It returns `status: "already_running"` with the existing request
metadata. The tool is annotated `readOnlyHint=false`,
`destructiveHint=false`, and `openWorldHint=true` because it changes the local
cache and causes the worker to read from ZenMoney.

`get_sync_status` accepts no arguments. It combines the control state with the
existing sync-status payload:

```json
{
  "state": "idle|pending|running|completed|failed",
  "request_id": "UUID or null",
  "mode": "incremental|full|null",
  "requested_at": 1787600000,
  "started_at": 1787600001,
  "finished_at": 1787600008,
  "failure_code": null,
  "last_sync_time": "2026-08-25T12:00:08",
  "staleness": "fresh",
  "cache_stats": {}
}
```

Control timestamps are Unix seconds. Existing sync-status fields retain their
current representation for compatibility. `get_sync_status` is annotated
`readOnlyHint=true`, `destructiveHint=false`, and `openWorldHint=false`.

Both tools are remote-only. The local `sync_data` tool and
`zenmoney://sync-status` resource remain unchanged.

## Control channel

Compose adds a small `zenmoney-sync-control` named volume mounted read-write
at `/sync-control` in the MCP and sync-worker containers. It is not mounted in
the tunnel client. The financial `/data` mount remains read-only in MCP and
read-write only in the worker.

The volume contains:

- `sync-state.json`, the single current or most recent request;
- `sync-state.lock`, a stable file used for an exclusive `fcntl.flock`.

Every read-modify-write transition holds the lock. State updates are written
to a same-directory temporary file and published with `os.replace`, so a
process crash cannot expose a partially written JSON document. The state file
contains only the fixed scalar fields shown in the tool contract; it contains
no credentials, API responses, financial data, paths, or exception text.

This is deliberately a single-flight channel, not a queue. No database,
broker, HTTP control service, or new dependency is introduced.

## Worker flow

The worker continues to sync immediately on startup and periodically at
`ZENMONEY_SYNC_INTERVAL_SECONDS`. While waiting, it checks the control state
once per second.

For `pending` or restart-leftover `running`, the worker:

1. records `running` and `started_at`;
2. calls the existing hardened sync with the requested `force_full` value;
3. records `completed` and `finished_at`, or `failed`, `finished_at`, and the
   fixed `failure_code: "sync_failed"`;
4. resumes the normal interval after the forced attempt, avoiding an immediate
   duplicate periodic sync.

Retrying a restart-leftover request is safe because upstream synchronization
is read-only and the hardened engine publishes a validated snapshot
atomically.

The one-second polling interval is an intentional single-user simplification.
It should become a socket or another notification mechanism only if measured
latency or polling cost matters.

## Validation and failures

Readers accept at most 4 KiB and validate the exact JSON shape, state enum,
UUID request ID, boolean `force_full`, and integer timestamps before acting.
Unknown, oversized, or malformed state is never executed. Status reports
`state: "failed"` and `failure_code: "invalid_sync_state"`; a new request is
rejected until the invalid state is removed by the operator.

Worker exceptions retain the existing sanitized logging contract. Neither the
control file nor MCP response includes exception text, an upstream response,
or a credential. A failed synchronization leaves the previous financial
snapshot and its `last_sync_time` intact.

## Verification

Implementation follows red-green-refactor with focused synthetic tests and no
live ZenMoney token:

- remote discovery exposes both tools with truthful per-tool annotations;
- local discovery remains backward compatible;
- concurrent force calls produce one request and return the same request ID;
- incremental and full requests return immediately and reach the worker with
  the requested mode;
- worker state transitions cover success, failure, and restart-leftover work;
- malformed control state fails closed without exposing its contents;
- status combines control state with the existing successful-sync timestamp;
- Compose keeps `/data:ro` in MCP, mounts only the control volume read-write,
  and preserves role-specific secret isolation.

Completion requires the full non-live pytest suite, Python compilation,
Compose rendering/validation, and the existing deployment tests. Live tunnel
and ZenMoney calls remain manual and must not be claimed from local evidence.

## Documentation impact

Update the README, remote runbook, and threat model: the remote surface is no
longer entirely read-only, but its only write capability is the bounded local
sync request. It still cannot write to ZenMoney, access the ZenMoney token, or
write the financial snapshot directly.
