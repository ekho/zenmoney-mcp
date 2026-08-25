# Universal ZenMoney User-Entity Changes Design

**Date:** 2026-08-25
**Status:** Approved in chat; written review pending
**Base:** PR #8, branch `codex/transaction-mutations`, commit `8682568`
**Supersedes:** `2026-08-25-transaction-mutations-design.md`

## Goal

Extend the confirmed two-step transaction mutation workflow into one safe
mutation subsystem for every ZenMoney user entity:

- `Account`;
- `Tag`;
- `Merchant`;
- `Reminder`;
- `ReminderMarker`;
- `Transaction`;
- `Budget`.

The subsystem supports create and update for all seven types. It supports only
safe semantic deletion: archive an account, mark a transaction or reminder
marker deleted, or clear a budget. Physical deletion through the ZenMoney
`deletion` array is out of scope.

The same contract is available through local stdio MCP and the private remote
ChatGPT deployment. Every write remains prepare -> review -> apply. Remote MCP
never receives a ZenMoney credential and never calls ZenMoney directly.

## API facts and uncertainty

The official ZenMoney wiki classifies the seven types above as user entities
and `Instrument`, `Company`, and `User` as read-only system entities. A Diff
request can contain arrays for several entity types at once. The server compares
each object's `changed` value with its version and returns subsequent changes.

The wiki was last edited in 2023. Open issues from 2024 document missing fields,
different deletion behavior, and a rejected UUID v4 when creating a Tag. The
documented model is therefore a starting point, not proof of the live write
contract.

The implementation must:

- preserve complete raw objects received from ZenMoney;
- send complete objects for create and update;
- allow only explicitly validated user-editable fields;
- fail closed for unverified fields and operations;
- use one mixed Diff request and never fall back to staged writes;
- verify every result through a subsequent sync;
- require a disposable test-profile live gate before claiming an entity
  operation is supported by the live API.

References:

- <https://github.com/zenmoney/ZenPlugins/wiki/ZenMoney-API>
- <https://github.com/zenmoney/ZenPlugins/issues/757>
- <https://github.com/zenmoney/ZenPlugins/issues/794>

## Scope matrix

| Entity | Create | Read | Update | Safe delete |
| --- | --- | --- | --- | --- |
| Account | yes | collection + exact resource | allowlisted fields; not `balance` | `archive=true` |
| Tag | yes | collection + exact resource | allowlisted fields | unsupported |
| Merchant | yes | collection + exact resource | allowlisted fields | unsupported |
| Reminder | yes | collection + exact resource | allowlisted fields | unsupported |
| ReminderMarker | yes | collection + exact resource | allowlisted fields | `state=deleted` |
| Transaction | yes | collection + exact resource | allowlisted fields | `deleted=true` |
| Budget | yes | collection + exact resource | allowlisted fields | unlock and zero amounts |

`Instrument`, `Company`, and `User` remain read-only. `purge`, restoration,
cross-owner moves, direct Account balance changes, automatic rollback, public
MCP ingress, and production deployment are out of scope.

## Approaches considered

### One generic public mutation tool

A single schema could accept every entity and operation. It would minimize tool
count, but its large discriminated union would be harder for MCP clients to
discover and construct correctly.

### Separate CRUD tools for every operation

`create_tag`, `update_tag`, `delete_tag`, and equivalent tools would be explicit
but add 21 mutation tools before read operations. The duplicated dispatch and
annotations would obscure the shared confirmation model.

### Chosen: entity preparation tools plus a mixed tool

Seven entity-specific preparation tools provide small strict schemas. One mixed
preparation tool handles cross-entity change sets. All preparation tools produce
the same proposal format and use one core. Proposal inspection and application
remain generic.

## MCP mutation contract

The public mutation surface is:

```text
prepare_account_changes
prepare_tag_changes
prepare_merchant_changes
prepare_reminder_changes
prepare_reminder_marker_changes
prepare_transaction_changes
prepare_budget_changes
prepare_mixed_changes
get_change_proposal
apply_changes
```

The transaction-only get/apply names in PR #8 are removed before merge. There
are no compatibility aliases because that surface has not been released.

Each entity-specific prepare tool accepts one to 100 operations for its type.
`prepare_mixed_changes` accepts one to 100 operations across all types. A
proposal may contain create, update, and safe-delete operations together.

Operation shapes are:

```json
{"operation":"create","ref":"new_food","value":{"title":"Eating out"}}
{"operation":"update","id":"entity-uuid","set":{"title":"New title"}}
{"operation":"delete","id":"entity-uuid"}
```

Mixed operations additionally require `entity`. Budget update/delete uses a
typed composite `key` containing `owner_user_id`, `tag`, and `date` instead of
`id`. Budget creation derives the same key from its value.

Create operations may expose a proposal-local `ref`. Reference-bearing fields
accept either an existing exact ID or `{"ref":"name"}`. During preparation,
the server assigns concrete entity IDs, resolves every local reference, and
stores only the resolved immutable result. Duplicate refs, unresolved refs,
cycles, incompatible reference types, and references to an operation later in a
dependency cycle reject the whole preparation.

ZenMoney IDs are generated by the MCP implementation, never accepted from the
caller for create. The exact generation format is capability-tested against the
test profile for each UUID-backed type. A generated ID is frozen in the
proposal and reused only for verification; ambiguous proposals are never
replayed.

`get_change_proposal` accepts only `proposal_id` and returns the bounded preview,
state, timestamps, failure code, and per-item result. `apply_changes` accepts
only `proposal_id`; it cannot alter the stored operations.

Annotations are truthful:

- prepare tools: `readOnlyHint=false`, `destructiveHint=false`,
  `openWorldHint=false`;
- get: `readOnlyHint=true`, `destructiveHint=false`,
  `openWorldHint=false`;
- apply: `readOnlyHint=false`, `destructiveHint=true`,
  `openWorldHint=true`.

## Read contract

CRUD read operations use entity-specific MCP resources rather than duplicating
list/get tools:

```text
zenmoney://accounts
zenmoney://tags
zenmoney://merchants
zenmoney://reminders
zenmoney://reminder-markers
zenmoney://transactions
zenmoney://budgets

zenmoney://accounts/{id}
zenmoney://tags/{id}
zenmoney://merchants/{id}
zenmoney://reminders/{id}
zenmoney://reminder-markers/{id}
zenmoney://transactions/{id}
zenmoney://budgets/{owner_user_id}/{date}/{tag_key}
```

Collection resource templates accept `limit`, `cursor`, and
`include_inactive`. The default limit is 50 and the maximum is 200, matching the
existing bounded transaction search. Responses contain `items` and an opaque
`next_cursor`. Each entity has a stable deterministic order; cursors encode the
last sort key and are validated before use. Invalid cursors fail closed.

Inactive means archived accounts, deleted transactions, deleted reminder
markers, and cleared budgets. Inactive rows are excluded by default. Exact
resources can return inactive rows and return a fixed not-found error for an
unknown key.

Resources return normalized user-visible data, not internal `raw_json`, tokens,
proposal contents, or upstream response bodies.

## Full-fidelity entity snapshot

Normalized tables remain the analytical read model. A new
`entity_raw` table stores complete upstream user-entity objects:

```text
entity_raw
  entity_type TEXT
  entity_key  TEXT
  raw_json    TEXT
  PRIMARY KEY (entity_type, entity_key)
```

`entity_key` is canonical JSON. UUID-backed entities use their ID. Budget uses
the composite `{user, tag, date}` identity. Callers never construct raw keys by
string concatenation.

Incremental sync merges an incoming partial object over the prior raw object,
then writes both normalized columns and canonical compact JSON. Unknown fields
survive. Full sync builds `entity_raw` from the full response and marks
`user_entity_raw_complete=1` only after atomic snapshot publication succeeds.

The existing transaction `raw_json` column is migrated into `entity_raw` when
present and retained as an unused compatibility column. New mutation code reads
only `entity_raw`. A migrated snapshot remains usable for analytics but cannot
prepare mutations until one successful full sync establishes complete raw data
for all user entity types.

## Proposal ledger

The separate mode-`0600` proposal SQLite database remains in the sync-control
volume. Its item schema becomes entity-neutral:

```text
proposals
  id, status, created_at, expires_at, requested_at,
  started_at, finished_at, failure_code

proposal_items
  proposal_id, position, entity_type, entity_key_json,
  operation, expected_changed, before_json, after_json, result
```

`before_json` is null for create. `expected_changed` is null for create and is
required for update/delete. The stored before/after documents contain only the
resolved user-visible preview, not complete raw objects. The full outgoing
objects are rebuilt and revalidated during apply from the current raw snapshot
and the frozen resolved operation.

Proposal lifecycle, bounds, and retention remain:

```text
prepared -> pending -> running -> applied
                              -> conflicted
                              -> failed
                              -> needs_review
prepared                     -> expired
```

- one to 100 items;
- prepared expiry after 24 hours;
- terminal retention for 30 days;
- idempotent apply by proposal ID;
- leftover `running` becomes `needs_review` after worker restart;
- no automatic replay or rollback.

## Entity validation

Every entity type has a small adapter with four responsibilities:

1. validate and normalize a create value;
2. apply and validate an update patch over a full raw object;
3. construct the safe-delete result where supported;
4. compare the post-sync object with the frozen expected result.

Adapters are registered in a fixed internal mapping. This is not a plugin API
and is not configurable at runtime.

All adapters enforce:

- strict schemas and no unknown input fields;
- finite bounded numbers and real ISO dates;
- existing or proposal-local typed references;
- owner compatibility;
- immutable `id`, `user`, `changed`, `created`, and unknown fields;
- complete-object validation before the write;
- no mutation of system entities.

If the full snapshot contains one User, create assigns it automatically. If it
contains several, create requires `owner_user_id`, which must exist in the same
snapshot. Update/delete preserves the original owner. Cross-entity references
must be owner-compatible.

Account `balance` cannot be changed. `startBalance` is create-only.
`creditLimit` and verified settings remain editable. Financial balance changes
must be expressed through transactions; a future balance-correction operation
would need a separate design.

Safe delete semantics are fixed:

- Account: set `archive=true`;
- Transaction: set `deleted=true`;
- ReminderMarker: set `state=deleted`;
- Budget: set both locks false and both amounts to zero;
- Tag, Merchant, Reminder: reject with `operation_not_supported`.

## Mixed apply flow

The executor performs:

1. Claim the exact proposal.
2. Run one incremental sync.
3. Require `user_entity_raw_complete=1`.
4. Check every existing entity's canonical identity and `expected_changed`.
5. Confirm every create identity is still absent.
6. Rebuild, resolve, and validate the entire mixed result against the fresh
   snapshot plus the proposal's planned creates.
7. If any item fails, send nothing and finish `conflicted` or `failed`.
8. Assign one client timestamp and send one `/v8/diff/` request containing all
   affected entity arrays.
9. Validate the response through the existing staging snapshot path.
10. Run another incremental sync.
11. Verify every create/update/delete result by canonical identity and expected
    user-visible fields.

The design does not claim server-side atomicity. One request prevents local
staged submission but cannot prove the server applies all arrays atomically.
Any transport failure after submission, malformed response, verification sync
failure, missing created entity, or partial/mixed result becomes
`needs_review`. It is never retried automatically.

## Local and remote execution

Local stdio prepare writes the proposal store; local apply runs the generic
executor synchronously with the local token.

Remote prepare writes the shared proposal store. Remote apply only changes
proposal state to `pending`. The credentialed worker claims one pending
proposal, executes it, and refreshes the shared financial snapshot. The worker
continues draining queued proposals one at a time. The remote MCP container has
no ZenMoney token and no direct ZenMoney egress.

Fixed failure codes cross MCP. Tokens, raw objects, Diff bodies, and proposal
previews never enter logs.

## Test-profile live gate

Automated tests use synthetic full objects and fake the network only at the
HTTP boundary. They cover schema migration, raw preservation, pagination,
entity validation, refs, ownership, mixed preflight, one-request payloads,
verification, recovery, MCP schemas, resources, and credential separation.

Live tests use only the token path below plus an explicitly configured numeric
test User ID:

```text
ZENMONEY_TEST_TOKEN_FILE=/Users/ekho/.config/zenmoney-mcp/test-token
ZENMONEY_TEST_USER_ID=123456
```

The token file is outside the repository, mode `0600`, and is never printed.
Before any write, the harness performs a full sync and requires the exact
configured test User ID. It creates objects with an obvious `MCP TEST` prefix.

`123456` is an example, not a default. The harness refuses to start unless the
variable is set and matches the only expected test profile owner.

The live sequence uses three isolated mixed proposals: create a minimal
dependency graph covering every user entity, update every created type, then
exercise the four safe-delete paths. Tag, Merchant, and Reminder fixtures
remain in the dedicated test profile because purge is out of scope. The harness
reports only IDs, operation states, and fixed error codes, never financial
payloads or the token.

No live test runs against production data. Passing synthetic tests or local
container checks does not substitute for the live capability matrix.

## Delivery

The work stays on `codex/transaction-mutations` and updates PR #8. Commits use
Conventional Commits and `Co-Authored-By`. Before push, the exact origin/refspec
is verified. Merge and production deployment remain separate, unauthorized
actions.
