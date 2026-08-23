# Remote MCP threat model

Scope: the private Compose deployment in `deploy/remote-mcp`. It is not a
public MCP endpoint and does not add an OAuth provider.

## Assets and boundaries

| Asset | Protection |
| --- | --- |
| ZenMoney token | File-mounted only into `zenmoney-sync`; ignored secret file with mode `0600`. |
| OpenAI tunnel runtime API key | File-mounted only into `tunnel-client`; ignored secret file with mode `0600`. |
| SQLite financial cache | Named volume; writable only by the sync worker and mounted read-only by the MCP server. |
| MCP outputs | Remote registry is read-only, bounded, and excludes sync/API-dependent tools. |

The seven trust boundaries are ZenMoney API → sync worker → SQLite volume →
MCP server → tunnel-client → OpenAI tunnel service → ChatGPT. ZenMoney and
OpenAI are external HTTPS services; every transition after the volume has a
distinct runtime role. The Compose `mcp_internal` network keeps the MCP server
off the host network, while `egress` carries outbound traffic. Network
separation is not a destination allowlist.

## Threats and mitigations

| Threat | Mitigation |
| --- | --- |
| Public exposure | No service has a `ports` mapping. The MCP HTTP server is internal-only; `tunnel-client` polls outbound over HTTPS. The operator must not add a public port or proxy. |
| Secret leakage | Compose secrets are file-backed, ignored, and mode `0600`; ZenMoney and tunnel keys are mounted into separate roles. Never put either value in `.env`, logs, images, or commits. |
| Logs containing finances | App logs use fixed non-sensitive fields; HTTP access logs and raw MCP request logging are disabled. Operators must not enable body logging or export unredacted diagnostics. |
| Malicious MCP input | The SDK owns protocol parsing; remote dispatch rejects unknown and excluded tools. Remote tools have read-only, non-destructive, closed-world annotations and strict schemas. |
| Stale cache | The worker performs an immediate sync then a configured interval; failed sync preserves the last readable snapshot. `/readyz` reports database readiness, not freshness. Operators choose and monitor a suitable interval. |
| Corrupted cache | Full sync uses atomic replacement/rollback. Remote reads use SQLite read-only mode and `/readyz` runs `PRAGMA quick_check`; online backup uses SQLite’s backup API. Restore is offline and validates `quick_check` first. |
| Container compromise | All roles use read-only roots, dropped capabilities, `no-new-privileges`, and `/tmp` tmpfs; Python roles run as UID/GID `10001`. The full tunnel image is not claimed to be non-root. Keep pinned images current and the host patched. |
| Unauthorized ChatGPT app access | Associate the tunnel only with approved Platform organizations and ChatGPT workspaces; Platform tunnel permissions and ChatGPT developer-mode access are separate. Operators review workspace membership and app access before Scan Tools or use. |

The operator owns host patching, secret rotation, firewall destination
allowlists (`api.zenmoney.ru:443`, `api.openai.com:443`, and optional
`mtls.api.openai.com:443`), Platform/ChatGPT access reviews, and the decision
to run the manual tunnel and ChatGPT checks.
