# Private remote MCP operations

This Compose deployment keeps the MCP server private. `zenmoney-mcp` serves
Streamable HTTP at `http://zenmoney-mcp:8000/mcp` only on `mcp_internal`;
`tunnel-client` makes the outbound connection to OpenAI. It is a single-user,
single-replica SQLite deployment: do not scale either application service.

## Prerequisites and Platform setup

- Docker Engine with Compose v2 and outbound HTTPS from the host.
- A ZenMoney token and an OpenAI tunnel runtime API key.
- A tunnel ID from [Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels).

In Platform tunnel settings, create or select the tunnel, then associate it
with the Platform organization that manages it and the target ChatGPT
workspace. The app creator needs **Tunnels Read + Use**; creating or editing a
tunnel needs **Tunnels Read + Manage**. ChatGPT developer-mode permission is
separate. These are manual Platform steps and were not run for this deployment;
see the [Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

The host firewall must allow the sync worker only to `api.zenmoney.ru:443` and
the tunnel client only to `api.openai.com:443` (or `mtls.api.openai.com:443`
when configured). Compose network separation does **not** enforce destination
allowlists.

## Configure and start

Run from the repository root. `.env` contains identifiers only and must never
contain the ZenMoney token. Both credentials use ignored file-backed Compose
secrets and are mounted into separate roles. Compose file-source remapping is
not implemented: service-level secret `uid`, `gid`, and `mode` settings do not
change the bind-mounted source. The source files must therefore be provisioned
for their runtime roles before startup: the control-plane key as `0:0` and the
ZenMoney token as `10001:10001`, both with mode `0400`.

```bash
cp deploy/remote-mcp/.env.example deploy/remote-mcp/.env
mkdir -p deploy/remote-mcp/secrets
umask 077
CONTROL_PLANE_API_KEY_TMP="$(mktemp)"
trap 'rm -f "$CONTROL_PLANE_API_KEY_TMP"' EXIT
printf '%s' "$CONTROL_PLANE_API_KEY" > "$CONTROL_PLANE_API_KEY_TMP"
sudo install -o 0 -g 0 -m 0400 "$CONTROL_PLANE_API_KEY_TMP" \
  deploy/remote-mcp/secrets/control-plane-api-key
rm -f "$CONTROL_PLANE_API_KEY_TMP"
trap - EXIT
ZENMONEY_TOKEN_TMP="$(mktemp)"
trap 'rm -f "$ZENMONEY_TOKEN_TMP"' EXIT
printf 'ZenMoney token: '
read -r -s ZENMONEY_TOKEN
printf '\n'
printf '%s' "$ZENMONEY_TOKEN" > "$ZENMONEY_TOKEN_TMP"
unset ZENMONEY_TOKEN
sudo install -o 10001 -g 10001 -m 0400 "$ZENMONEY_TOKEN_TMP" \
  deploy/remote-mcp/secrets/zenmoney-token
rm -f "$ZENMONEY_TOKEN_TMP"
trap - EXIT
```

Set `CONTROL_PLANE_TUNNEL_ID` in `deploy/remote-mcp/.env` to the value from
Platform, then build and start:

```bash
docker build -t zenmoney-mcp:remote-test .
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml up -d
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml ps
```

The pinned images are `python:3.12.14-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134`,
`ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1`,
and `ghcr.io/openai/tunnel-client:v0.0.12@sha256:b1e9eb675e6a64775685c323c2af8c2810ea14e1a27c8ce4c68f2994cd7c5e8e`.

To review or update the tunnel-client pin, compare the configured tag with the
[official releases](https://github.com/openai/tunnel-client/releases), read the
candidate release notes, then resolve its multi-architecture index digest:

```bash
TUNNEL_TAG=v0.0.12
docker buildx imagetools inspect "ghcr.io/openai/tunnel-client:$TUNNEL_TAG"
```

Copy the top-level `Digest: sha256:...` value—not a platform child digest—into
the tag-plus-digest image reference in `compose.yaml` and `TUNNEL_IMAGE` in the
deployment test. Verify the update with `docker pull` of that exact reference,
`docker compose ... config -q`, the deployment tests, and the runtime smoke
before rollout.

## Verify

`zenmoney-mcp` publishes no host port. Check its internal health from the
application container:

```bash
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml exec zenmoney-mcp \
  python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8000/healthz').read().decode())"
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml exec zenmoney-mcp \
  python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8000/readyz').read().decode())"
```

Inspect non-sensitive service logs without enabling request/body logging:

```bash
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml logs --tail=100 \
  zenmoney-mcp zenmoney-sync tunnel-client
```

**Manual, not run:** the pinned `v0.0.12` client accepts the Compose
direct-config flags for `doctor`. Load the non-secret tunnel ID from `.env` and
run a one-shot diagnostic container; it receives the existing Compose secret
mount but does not start a tunnel daemon:

```bash
set -a
. deploy/remote-mcp/.env
set +a
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml run --rm --no-deps \
  --entrypoint /usr/bin/tunnel-client tunnel-client doctor --explain \
  --control-plane.tunnel-id="$CONTROL_PLANE_TUNNEL_ID" \
  --control-plane.api-key=file:/run/secrets/control-plane-api-key \
  --mcp.server-url=http://zenmoney-mcp:8000/mcp \
  --health.listen-addr=:0 \
  --log.level=info \
  --log.format=json
```

A doctor result is not part of CI and requires the real runtime key and tunnel
ID.

**Manual, not run:** in ChatGPT, create a developer-mode app, select
**Tunnel** as its connection, select or paste this tunnel ID, then use **Scan
Tools**. Confirm that `sync_data` and `suggest_category` are absent;
`force_sync` is non-read-only, non-destructive, and open-world;
all `prepare_*_changes` tools are non-read-only, non-destructive, and
closed-world; `apply_changes` is non-read-only, destructive, and
open-world; and analytical tools remain read-only and closed-world. Call
`get_sync_status`, then make one read-only analytical call. This deployment has
no public-plugin submission path.

## Sync, backup, restore, and rollback

The worker syncs immediately and then every
`ZENMONEY_SYNC_INTERVAL_SECONDS` seconds (default `900`). For one deliberate
sync without starting the tunnel client:

```bash
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml run --rm --no-deps \
  --entrypoint zenmoney-sync-once zenmoney-sync
```

### Remote synchronization and entity-change control

The remote `force_sync` tool accepts `force_full` (default `false`) and returns
immediately with `accepted`, a request ID, mode, and request timestamp. It does
not hold the MCP call open while ZenMoney responds. If a request is already
pending or running, another call returns `already_running` with that request
ID instead of creating a queue.

Use `get_sync_status` to observe `pending`, `running`, `completed`, or `failed`,
plus the last successful cache sync time. A failed forced request reports only
the fixed `sync_failed` code and leaves the previous snapshot readable. A full
request may take longer than an incremental request; completion is determined
only from `get_sync_status`, not from the initial `force_sync` response.

The control volume contains no credentials. It does contain entity-change
previews and results, so protect it as financial data and do not include it in
diagnostics. If
`get_sync_status` reports `invalid_sync_state`, inspect service health and
fixed-field logs first. To discard only the invalid control state and allow a
new request, run:

```bash
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml exec -T zenmoney-sync \
  rm -f /sync-control/sync-state.json
```

This does not delete or modify `/data/zenmoney.db`. A leftover `running`
request after a worker restart is retried automatically.

Entity changes use a separate two-call confirmation. First call the matching
entity-specific `prepare_*_changes` tool, or `prepare_mixed_changes`, and review
every returned before/after value. Then pass only its `proposal_id` to
`apply_changes`. The remote MCP
stores the confirmation and returns without calling ZenMoney; the credentialed
worker processes one queued proposal at a time. Poll
`get_change_proposal` until it reaches `applied`, `conflicted`,
`failed`, or `needs_review`.

Preparation requires a successful full sync. A proposal expires after 24 hours
if it is not confirmed. Terminal records are removed after 30 days. The worker
never automatically replays a proposal left `running` after restart: it marks
the result `needs_review` because the preceding ZenMoney write may have
succeeded. Resolve that state by inspecting ZenMoney and preparing a new
proposal for any remaining changes.

Create and update cover Account, Tag, Merchant, Reminder, ReminderMarker,
Transaction, and Budget. Safe delete is limited to Account archive, Transaction
and ReminderMarker semantic deletion, and Budget clearing. Purge is unavailable.
Related creates are sent in dependency layers; a failure after any layer leaves
the whole proposal in `needs_review` and is never replayed automatically.

Create an online backup through SQLite’s backup API, not `cp` of a live WAL
database. Backups contain sensitive financial data: keep them encrypted at
rest, access-controlled, and outside the Compose project. This uses the already
running `zenmoney-sync` container, then copies the completed temporary volume
file to a host-owned temporary file before atomically publishing it:

```bash
set -e
BACKUP_DIR=/absolute/path/to/zenmoney-backups
BACKUP_NAME="zenmoney-$(date +%Y%m%d-%H%M%S).db"
VOLUME_BACKUP="/data/.${BACKUP_NAME}.$$"
HOST_TMP="$BACKUP_DIR/.${BACKUP_NAME}.tmp"
install -d -m 700 "$BACKUP_DIR"
cleanup() {
  rm -f "$HOST_TMP" || true
  docker compose --env-file deploy/remote-mcp/.env \
    -f deploy/remote-mcp/compose.yaml exec -T zenmoney-sync \
    /bin/rm -f "$VOLUME_BACKUP" || true
}
trap cleanup EXIT
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml exec -T zenmoney-sync \
  zenmoney-db-backup "$VOLUME_BACKUP" --source /data/zenmoney.db
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml cp \
  "zenmoney-sync:$VOLUME_BACKUP" "$HOST_TMP"
chmod 600 "$HOST_TMP"
mv "$HOST_TMP" "$BACKUP_DIR/$BACKUP_NAME"
trap - EXIT
cleanup
```

Restore is offline. Set `BACKUP` to the absolute path created above, stop all
roles, then bind the backup read-only. The command stages a copy on the named
volume. It first normalizes the staged file to the `DELETE` rollback journal.
The shared snapshot validator then checks `PRAGMA quick_check`, every synced
entity table, the exact `sync_meta` schema, and its supported `schema_version`
before the command atomically replaces the target and removes stale sidecars.
It never copies directly over a live database.

```bash
set -e
BACKUP=/absolute/path/to/zenmoney-backups/zenmoney-YYYYmmdd-HHMMSS.db
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml down
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml run --rm --no-deps --entrypoint python \
  --volume "$BACKUP:/restore/backup.db:ro" zenmoney-sync -c "import os, shutil; from zenmoney_mcp.hardened_database import HardenedDatabase, validate_snapshot; backup='/restore/backup.db'; stage='/data/zenmoney.restore-staging.db'; target='/data/zenmoney.db'; os.path.exists(stage) and os.unlink(stage); shutil.copyfile(backup, stage); os.chmod(stage, 0o600); writer=HardenedDatabase(stage, journal_mode='DELETE'); writer.connect(); writer.close(); db=HardenedDatabase(stage, read_only=True); assert validate_snapshot(db.connect()); db.close(); os.replace(stage, target); os.chmod(target, 0o600); [os.unlink(target + suffix) for suffix in ('-wal', '-shm') if os.path.exists(target + suffix)]"
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml up -d
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml exec zenmoney-mcp \
  python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8000/readyz').read().decode())"
```

`docker compose down` removes containers and networks but preserves the named
SQLite volume. `docker compose down -v` also deletes that volume and is
destructive; use it only when deliberately discarding the cache and backups.
For a deployment rollback, stop with `down`, restore the prior immutable
application image value in `.env`, then `up -d`; use the offline database
restore above when the cache also needs rollback.
