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

Run from the repository root. `.env` contains identifiers only; secret values
live in ignored files and must never be committed.

```bash
cp deploy/remote-mcp/.env.example deploy/remote-mcp/.env
mkdir -p deploy/remote-mcp/secrets
printf '%s' "$ZENMONEY_TOKEN" > deploy/remote-mcp/secrets/zenmoney-token
printf '%s' "$CONTROL_PLANE_API_KEY" > deploy/remote-mcp/secrets/control-plane-api-key
chmod 600 deploy/remote-mcp/secrets/zenmoney-token \
  deploy/remote-mcp/secrets/control-plane-api-key
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

**Manual, not run:** after the client is running, diagnose a named client
profile with the documented command:

```bash
tunnel-client doctor --profile <profile> --explain
```

Compose supplies direct flags rather than a named profile, so first inspect
`tunnel-client doctor --help` in the pinned image before adapting this command.
A doctor result is not part of CI and requires the real runtime key and tunnel
ID.

**Manual, not run:** in ChatGPT, create a developer-mode app, select
**Tunnel** as its connection, select or paste this tunnel ID, then use **Scan
Tools**. Confirm that every listed tool is read-only and that `sync_data` and
`suggest_category` are absent. Finally make one read-only analytical call.
This deployment has no public-plugin submission path.

## Sync, backup, restore, and rollback

The worker syncs immediately and then every
`ZENMONEY_SYNC_INTERVAL_SECONDS` seconds (default `900`). For one deliberate
sync without starting the tunnel client:

```bash
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml run --rm --no-deps \
  --entrypoint zenmoney-sync-once zenmoney-sync
```

Create an online backup through SQLite’s backup API, not `cp` of a live WAL
database. This places the backup on the named data volume:

```bash
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml exec zenmoney-sync \
  zenmoney-db-backup /data/backup-$(date +%Y%m%d-%H%M%S).db \
  --source /data/zenmoney.db
```

Restore is offline. Stop all roles, validate the selected backup with
`PRAGMA quick_check`, replace the database, set its mode to `0600`, and start
again. Replace `BACKUP` with the backup path on `/data`.

```bash
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml down
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml run --rm --no-deps --entrypoint python \
  zenmoney-sync -c "import os, shutil, sqlite3; backup='BACKUP'; conn=sqlite3.connect(f'file:{backup}?mode=ro', uri=True); assert conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'; conn.close(); shutil.copyfile(backup, '/data/zenmoney.db'); os.chmod('/data/zenmoney.db', 0o600)"
docker compose --env-file deploy/remote-mcp/.env \
  -f deploy/remote-mcp/compose.yaml up -d
```

`docker compose down` removes containers and networks but preserves the named
SQLite volume. `docker compose down -v` also deletes that volume and is
destructive; use it only when deliberately discarding the cache and backups.
For a deployment rollback, stop with `down`, restore the prior immutable
application image value in `.env`, then `up -d`; use the offline database
restore above when the cache also needs rollback.
