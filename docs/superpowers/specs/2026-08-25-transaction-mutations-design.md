# Transaction Mutations Design

**Date:** 2026-08-25
**Status:** Approved in chat
**Base:** `origin/main` at `3a45b1041c6b77b940ff884bd9f26c501b02d10b`

## Goal

Add explicit two-step editing of existing ZenMoney transactions to both local
stdio MCP and the private remote ChatGPT deployment. A user first prepares and
reviews an immutable batch, then applies that exact proposal by ID.

Creation of transactions, editing other ZenMoney entity types, automatic
rollback, public ingress, and direct ZenMoney credentials in the remote MCP
server are out of scope.

## API facts and uncertainty

ZenMoney's documented `/v8/diff/` contract accepts changed user entities in the
same request used for synchronization. It compares each entity's `changed`
timestamp with the server version and returns server-side changes since the
request's `serverTimestamp`. Transactions contain a `deleted` flag and can also
appear in the generic `deletion` list.

The public wiki was last edited in 2023 and an open 2024 issue records fields
that are missing or inaccurate in that documentation. In particular, the issue
observed transaction deletion returning a transaction with `deleted=true`
rather than a `deletion` entry. The implementation therefore:

- preserves the full transaction object received from ZenMoney;
- sends full objects rather than partial patches;
- supports deletion only as `deleted: false -> true`;
- does not claim that deletion is reversible;
- does not claim that a batch is atomically accepted by ZenMoney;
- requires a separate disposable-transaction live gate before production use.

References:

- <https://github.com/zenmoney/ZenPlugins/wiki/ZenMoney-API>
- <https://github.com/zenmoney/ZenPlugins/issues/757>

## Architecture

The existing financial SQLite snapshot remains the analytical read model. The
`transactions` table gains `raw_json`, containing the complete object last
received from ZenMoney. Normalized columns continue to serve SQL analytics.
`raw_json` exists solely to preserve fields that this repository does not
otherwise model when constructing a write.

A separate SQLite database in the existing sync-control volume stores mutation
proposals. It contains no token. The proposal database uses two tables:

```text
proposals
  id, status, created_at, expires_at, requested_at,
  started_at, finished_at, failure_code

proposal_items
  proposal_id, position, transaction_id, expected_changed,
  patch_json, before_json, after_json, result
```

The two runtimes share the proposal model and mutation executor:

```text
stdio prepare -> proposal store
stdio apply   -> MutationExecutor -> ZenMoney -> refreshed snapshot

remote prepare -> proposal store
remote apply   -> status=pending
sync worker    -> claim -> MutationExecutor -> ZenMoney -> refreshed snapshot
```

The remote MCP server retains no ZenMoney credential and has no direct ZenMoney
egress. It can only create, inspect, and enqueue strictly validated proposals.
The credentialed sync worker validates proposals again and is the sole remote
writer.

## Full-fidelity snapshot migration

The supported snapshot schema version increments. Migration adds nullable
`transactions.raw_json`; it does not invent raw objects for existing rows.

Every transaction upsert stores canonical compact JSON. When an incremental
response omits fields, the incoming object is merged over the previously stored
raw object before normalized columns and `raw_json` are updated. Unknown fields
therefore survive later syncs.

A successful full sync sets `transaction_raw_complete=1` in `sync_meta` only
after the staging snapshot has received and validated the full response. Write
tools fail closed until that marker exists. A migrated database consequently
continues serving read-only analytics but requires one successful full sync
before transaction proposals can be prepared.

## MCP contract

The feature adds exactly three tools:

```text
prepare_transaction_changes(changes[])
get_transaction_change_proposal(proposal_id)
apply_transaction_changes(proposal_id)
```

`changes` contains one to 100 objects. Each object has an exact
`transaction_id` and a non-empty `set` object. Duplicate transaction IDs are
rejected. Dynamic selectors and saved search filters are not accepted.

`prepare_transaction_changes` reads the current full objects, validates the
whole proposed result, and stores all proposal rows in one SQLite transaction.
It returns the proposal ID, timestamps, status, and a per-transaction preview of
changed user-visible fields only.

`get_transaction_change_proposal` returns the same bounded preview plus current
state and per-item results. It never returns `raw_json` or a ZenMoney response
body.

`apply_transaction_changes` accepts only `proposal_id`. It cannot replace or
extend the stored patch. Local stdio awaits execution. Remote MCP moves a
prepared proposal to `pending`; the worker executes it asynchronously and the
caller reads progress with the get tool.

MCP annotations are truthful:

- prepare: `readOnlyHint=false`, `destructiveHint=false`, `openWorldHint=false`;
- get: `readOnlyHint=true`, `destructiveHint=false`, `openWorldHint=false`;
- apply: `readOnlyHint=false`, `destructiveHint=true`, `openWorldHint=true`.

## Editable fields

The patch allowlist is:

- `date`;
- `income`, `outcome`;
- `incomeAccount`, `outcomeAccount`;
- `incomeInstrument`, `outcomeInstrument`;
- `tag`;
- `merchant`, `payee`, `comment`;
- `opIncome`, `opOutcome`;
- `opIncomeInstrument`, `opOutcomeInstrument`;
- `latitude`, `longitude`;
- `deleted`, with `true` as the only accepted patch value.

The executor validates both the patch and the complete resulting transaction:

- IDs refer to cached accounts, instruments, tags, and merchants;
- amounts are finite and non-negative, and at least one side is positive unless
  the transaction is being deleted;
- non-debt account instruments match their corresponding transaction
  instruments;
- original-operation amount and instrument fields are paired;
- dates are real ISO calendar dates;
- latitude and longitude are within documented ranges;
- deletion cannot be used to restore a transaction.

The following source, identity, and synchronization fields are immutable:
`id`, `user`, `created`, `changed`, `originalPayee`, `hold`, `mcc`, bank IDs,
`source`, `viewed`, `qrCode`, `reminderMarker`, and every unknown field. They are
copied from the fresh raw object unchanged.

## Proposal lifecycle and retention

Proposal states are:

```text
prepared -> pending -> running -> applied
                              -> conflicted
                              -> failed
                              -> needs_review
prepared                     -> expired
```

Local execution may move directly from `prepared` to `running`. Repeated apply
calls are idempotent by proposal ID: they return current or terminal state and
never create a second proposal.

Prepared proposals expire after 24 hours. Terminal proposals and their bounded
previews are retained for 30 days, then deleted. Cleanup runs opportunistically
when the proposal store is opened or mutated. These fixed values are not
configurable in the first version.

## Apply flow

The executor performs this sequence:

1. Claim the exact proposal.
2. Run an incremental sync.
3. Require every transaction to exist with full raw data and the recorded
   `expected_changed` value.
4. Reapply and revalidate every patch against the fresh raw objects.
5. Assign one current client timestamp as `changed` and send all full
   transaction objects in one `/v8/diff/` request.
6. Validate and apply the response through the existing atomic staging path.
7. Run another incremental sync.
8. Compare every requested field with the resulting full objects and record a
   per-item outcome.

If any item fails preflight, no write request is sent and the whole proposal is
`conflicted` or `failed`. Sending one request avoids local partial submission,
but the design does not assert server-side transactionality.

## Failure and recovery behavior

Definite validation or local persistence failures before the API call are
`failed`. A stale or missing transaction discovered during preflight is
`conflicted` and prevents the entire request.

Any timeout or transport failure after the write attempt begins, worker death
while a proposal is `running`, malformed success response, or mixed verification
result becomes `needs_review`. The worker marks leftover `running` proposals
`needs_review` at startup and never blindly resends them.

Per-item results are `applied`, `unchanged`, `conflicted`, or `unknown`.
Automatic rollback is prohibited because it is another destructive write that
could overwrite a newer change made in ZenMoney. Error strings exposed through
MCP are fixed failure codes; upstream bodies and financial payloads are not
logged.

## Security and operations

The proposal database is mode `0600` and lives beside the existing sync-control
state in its named volume. Both the remote MCP server and sync worker mount that
volume read-write. The financial snapshot remains read-only in the MCP
container. Only the worker mounts the ZenMoney token secret.

The proposal store contains transaction IDs and changed-field previews, so it is
financial data. It follows the same backup, diagnostic, logging, and access
handling as the main snapshot. Operators must not print or export it in support
logs.

Deployment readiness for analytical tools remains snapshot-based. Mutation
readiness is reported by the mutation tools and additionally requires
`transaction_raw_complete=1`; an old snapshot does not make the whole MCP server
unready.

## Verification

Automated tests use synthetic databases and mocked HTTP only at the ZenMoney
network boundary. They cover:

- schema migration, raw JSON preservation, and full-sync readiness;
- patch allowlist and complete-object validation;
- immutable batch creation, bounds, duplicate IDs, expiry, and retention;
- conflict handling and absence of a write on stale batches;
- successful full-object payload construction and verification;
- idempotent apply and ambiguous-result `needs_review` behavior;
- worker recovery and remote queue processing;
- stdio and remote MCP schemas, annotations, dispatch, and bounded outputs;
- Compose credential separation, volume mounts, and sanitized logs.

The live gate is manual and separately authorized. The operator creates a
disposable transaction in the ZenMoney app, then uses MCP to change its
user-visible fields, verifies the result in the app, and finally marks only that
disposable transaction deleted. No real financial record is used for smoke
testing.

## Delivery

Implementation is isolated on `codex/transaction-mutations`, committed with
Conventional Commits and `Co-Authored-By`, pushed explicitly to `origin`, and
published as a GitHub pull request against `main`. Merge and production
deployment are not included.
