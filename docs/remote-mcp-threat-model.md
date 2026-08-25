# Remote MCP threat model

Scope: the private Compose deployment in `deploy/remote-mcp`. It is not a
public MCP endpoint and does not add an OAuth provider.

## Assets and boundaries

| Asset | Protection |
| --- | --- |
| ZenMoney token | Ignored file-backed Compose secret mounted only into `zenmoney-sync`; the host source is owned by UID/GID `10001`, mode `0400`. |
| OpenAI tunnel runtime API key | File-mounted only into `tunnel-client`; ignored secret file with mode `0600`. |
| SQLite financial cache | Named volume; writable only by the sync worker and mounted read-only by the MCP server. |
| Control state and proposal ledger | Separate named volume, writable only by MCP and the sync worker. Sync control is bounded JSON; the SQLite proposal ledger contains sensitive before/after transaction values but no credentials. |
| MCP outputs | Financial analytics are read-only and bounded. `force_sync` can request a cache refresh. Transaction writes require a persisted preview followed by confirmation of its exact proposal ID. The MCP role cannot access credentials or call ZenMoney. |

The nine trust-boundary crossings are:

1. operator secret provisioning → runtime roles;
2. ZenMoney API ↔ sync worker;
3. sync worker → SQLite RW volume;
4. MCP server → sync-control RW volume;
5. sync-control volume ↔ sync worker;
6. SQLite RO volume → MCP server;
7. MCP server → tunnel-client over `mcp_internal`;
8. tunnel-client → OpenAI tunnel service over outbound HTTPS;
9. OpenAI tunnel service → authorized ChatGPT workspace/account.

The Compose `mcp_internal` network keeps the MCP server off the host network,
while `egress` carries outbound traffic. Network separation is not a
destination allowlist.

## Threats and mitigations

| Threat | Mitigation |
| --- | --- |
| Public exposure | No service has a `ports` mapping. The MCP HTTP server is internal-only; `tunnel-client` polls outbound over HTTPS. The operator must not add a public port or proxy. |
| Secret leakage | Both credentials are ignored file-backed secrets mounted into separate roles. Because Compose does not remap file-source ownership, the ZenMoney source itself is UID/GID `10001`, mode `0400`; the tunnel key remains mode `0600`. Never put either value in `.env`, logs, images, or commits. |
| Logs containing finances | App logs use fixed non-sensitive fields; HTTP access logs and raw MCP request logging are disabled. Operators must not enable body logging or export unredacted diagnostics. |
| Malicious MCP input | The SDK owns protocol parsing; remote dispatch rejects unknown and excluded tools. Mutation schemas allow only named user-editable fields, exact transaction IDs, and batches of at most 100. Preparation is non-destructive; apply is truthfully destructive/open-world and accepts only a proposal UUID. |
| Sync-trigger abuse | Control state is single-flight, at most 4 KiB, strictly validated, and atomically replaced under a file lock. Repeated requests return the existing request ID; MCP cannot supply paths, commands, credentials, or request bodies. |
| Stale or partial transaction data | Preparation is disabled until a full sync has stored complete raw transaction objects. Apply performs a fresh sync and rejects the full batch if any `changed` timestamp differs. Untouched fields are preserved in the outgoing objects. |
| Ambiguous write result | A write error, failed verification sync, verification mismatch, or worker restart while a proposal is `running` becomes `needs_review`. Such proposals are never replayed automatically. |
| Corrupted control state | Unknown or malformed state is never executed. Tools return the fixed `invalid_sync_state` code until the operator removes the invalid state file. |
| Stale cache | The worker performs an immediate sync then a configured interval; `get_sync_status` exposes forced-request progress and the last successful sync. Failed sync preserves the last readable snapshot. `/readyz` reports database readiness, not freshness. Operators choose and monitor a suitable interval. |
| Corrupted cache | Full sync uses atomic replacement/rollback. Remote readiness, backup, and offline restore share one validator for `quick_check`, every synced entity table, and the supported `sync_meta` schema/version. Online backup uses SQLite’s backup API. |
| Container compromise | All roles use read-only roots, dropped capabilities, `no-new-privileges`, and `/tmp` tmpfs; Python roles run as UID/GID `10001`. A compromised MCP still cannot write the financial cache, read the ZenMoney credential, or call ZenMoney directly, but it can read or corrupt proposals on the writable control volume and queue confirmed changes. The worker revalidates snapshot versions and allowed fields before writing. The full tunnel image is not claimed to be non-root. Keep pinned images current and the host patched. |
| Unauthorized ChatGPT app access | Associate the tunnel only with approved Platform organizations and ChatGPT workspaces; Platform tunnel permissions and ChatGPT developer-mode access are separate. Operators review workspace membership and app access before Scan Tools or use. |

The operator owns host patching, secret rotation, firewall destination
allowlists (`api.zenmoney.ru:443`, `api.openai.com:443`, and optional
`mtls.api.openai.com:443`), Platform/ChatGPT access reviews, and the decision
to run the manual tunnel and ChatGPT checks.
