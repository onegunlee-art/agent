# AI Company OS Implemented Architecture

## V0.2 execution additions

- WorkOrder execution uses a short SQLite claim, `execution_id`, fence token,
  expiry, and a short finalization transaction. Model work never holds a write
  transaction open.
- Expired `WORKSPACE_ONLY` claims return to retryable state; `EXTERNAL` claims
  require manual recovery. Late results cannot overwrite a newer lease.
- The Codex CLI executor runs once in a validated `wo/<id>` Git worktree with
  `workspace-write`, Windows elevated sandboxing, a bounded process tree, a
  safe environment allowlist, and a separate acceptance command.
- Each model execution automatically emits separate, append-only
  `RUN_REPRODUCIBILITY` Evidence containing the exact built instructions,
  acceptance command, starting branch/HEAD, sparse-checkout patterns, resulting
  diff, and changed-file SHA-256 manifest. Retrospective records are explicitly
  labeled `RETROACTIVE_OBSERVATION` and are not presented as execution-time
  capture. A retrospective record emits a separate append-only
  `EVIDENCE_BACKFILLED` Event with its reconstruction basis and original
  Evidence IDs; original Evidence rows and files remain untouched.
- Functional outcome, USD cost status, and measurable usage status are separate.
  A ChatGPT-login run can be `DONE` with `cost_status=UNAVAILABLE` only when its
  one-process-call, token, and time limits pass. The time limit kills the
  process tree during execution. Token accounting is input plus output and is
  enforced as post-execution rejection because the CLI does not expose a
  mid-generation cancellation meter.
- Rubric reports support weighted deterministic checks, critical hard failures,
  fail-closed judge criteria, and hash-bound Evidence. DRAFT reports are marked
  `official=false` until CEO approval.
- Canonical SQLite state defaults outside the repository. SQLite-only backup
  uses the online backup API, a SHA-256 sidecar, integrity check, table counts,
  and restore to a new path only. Full recovery additionally creates a ZIP
  manifest containing the database and every ledger-referenced Evidence,
  Artifact, verifier, context manifest, council response, and review handoff.
  Restore accepts only a verified bundle and empty destination paths.
- Loopback-only preview and dashboard servers provide the browser surfaces;
  the dashboard uses read-only SQLite snapshots and hash-bound approval or
  revision actions.
- The preview answer engine is deterministic FAQ retrieval, not an LLM answer
  generator. Codex is the coding executor that changes the preview program.

## Vertical slice

```text
Idea
  -> CTO/CPO/CMO request files
  -> validated response ingestion
  -> deterministic Council Compiler
  -> VentureContract
  -> FirstPrinciplesGate
  -> approval-state record
  -> venture-scoped workspace + Context Manifest
  -> first WorkOrder + immutable verifier hash
  -> ExecutorPort
  -> deterministic verification
  -> Evidence + append-only Event
  -> ReviewRequest JSON/Markdown
  -> WAITING_FOR_OPUS
  -> restart and resume from SQLite
```

The compiler requires all three active role responses and deterministically
merges role-owned fields: CTO owns technical constraints and verification,
CPO owns the observable problem and metric, and CMO owns the validating
experiment and strategy fields. Shared-field disagreement creates an Inbox
artifact and stops compilation until `company council resolve` receives a
complete CEO-supplied contract. A corrected role response creates a new
version and marks the prior response `SUPERSEDED`. Every role's unowned fields
remain in provenance as value hashes. A non-owner value that differs from the
owner's value is surfaced as an advisory Inbox item with both source values;
the owner value remains authoritative.

## Component boundaries

- `roles.py`: the only three RoleSpecs and deterministic synthetic test data.
- `council.py`: pure role-field ownership and deterministic contract synthesis.
- `first_principles.py`: pure structural validation for FP_LITE, FP_STANDARD,
  and FP_FULL. FACT references must resolve to canonical trusted Evidence and
  source types use a positive allowlist. The automatic synthetic fixture is
  exact-statement scoped; explicit CEO-registered documents are hash-bound
  general-document grants. Policy supplies the minimum decision level. The
  gate does not determine real-world truth or authenticate the claimed CEO.
- `source_snapshot.py`: local Git commit/tree capture and a deterministic
  SHA-256 manifest of tracked source bytes. Review binding is fail-closed: the
  tracked source must match HEAD, untracked source and unsafe index flags are
  rejected, project-local bytecode plus tracked symlinks/gitlinks are rejected,
  and only explicit runtime/cache ignored roots are tolerated. The Python
  interpreter and installed dependency environment are a separate trusted
  boundary and are not attested by this source digest.
- `storage.py`: SQLite schema, transactions, idempotency, global stop state,
  immutable Event ledger, and JSONL export.
- `application.py`: state transitions, Venture scaffolding, logical scoping,
  handoffs, execution, verification, Evidence, review, and resume logic.
- `verifier.py`: built-in synthetic exact-text verifier and SHA-256 integrity
  helpers.
- `cli.py`: Windows-friendly `argparse` interface.
- `fakes.py`: deterministic test doubles only; never represented as real
  executive or Opus execution.

## Canonical state

SQLite owns Ideas, council responses, Contracts, Ventures, Assumptions,
Metrics, Experiments, WorkOrders, Runs, Reviews, required review changes,
change resolutions, Evidence metadata,
Decisions, Approvals, Artifact metadata, Events, global stop state, and
idempotency state. Every state transition that emits an Event writes both in
one transaction.

The filesystem owns actual artifacts, Context Manifests, council and review
handoffs, and verifier output. Venture workspaces are built under a unique
staging directory, committed in SQLite with system-generated IDs, and then
promoted atomically. A failed promotion preserves staging so the same
idempotency key can recover it after restart. SQLite paths are stored relative
to the selected root and resolved with containment checks. Model IDs are
preserved only as Venture-scoped `external_ref` values.

## Recovery and integrity

- `company stop` is durable and blocks new execution, while status, export,
  review ingestion, and `company resume` remain available.
- After a verified Run and before review creation, restart resumes at review
  preparation without executing the WorkOrder again.
- Duplicate commands use an idempotency key plus a canonical payload hash.
  Reusing a key for a different payload is rejected.
- SQLite foreign keys, transactions, busy timeout, WAL for file databases,
  and rollback prevent partial canonical state.
- Event UPDATE and DELETE are rejected by database triggers.
- A verifier hash mismatch creates an `INVALIDATED` Run, preserves untrusted
  Evidence metadata, and emits `VERIFIER_TAMPER_DETECTED`.
- Executor output must equal the WorkOrder's declared artifact path; an
  unrelated workspace file cannot become trusted Evidence.
- Review preparation rehashes Evidence and Artifact files. A schema-v2 request
  contains the WorkOrder objective/acceptance, full verifier definition and
  output, bounded inline artifact content (or a hashed attachment), the source
  commit, and a deterministic source-tree SHA-256. Both the canonical JSON and
  human-facing Markdown handoff hashes are checked again when a ReviewResult
  is ingested.

## Review boundary

A real run creates an Opus ReviewRequest and stops in `WAITING_FOR_OPUS`.
Only a `user_supplied` response can continue the CLI workflow. FakeReviewer is
accepted only through an explicit in-process test policy and cannot be enabled
by a CLI flag or environment variable.

`CHANGES_REQUIRED` creates one durable OPEN record per change ID. Repair needs
an explicit manifest that covers every ID with the current commit, source-tree
SHA-256, and canonical trusted Evidence IDs whose files are rehashed. Every
change must cite its own `TEST_RESULT`, bound to the origin Review, change ID,
test node IDs, result-file SHA-256, and current source snapshot. An unchanged
retry is rejected. Only a UTF-8 JSON receipt with exit code zero and explicit
PASSED outcomes for every selected node is eligible; opaque or failed output
cannot be trusted repair Evidence. A passing
repair moves the WorkOrder to `AWAITING_REREVIEW`, records the resolutions as
SUBMITTED, and creates a new bound ReviewRequest. Only a reviewer PASS on that
new request marks the changes VERIFIED and the WorkOrder COMPLETED.

Each Review uses
`var/handoffs/reviews/<work_order_id>/<review_id>/`, so a later request or
response cannot overwrite prior audit material.

Schema-v2 ReviewRequests state the exact required response schema version,
reviewed commit, and reviewed tree SHA-256 under
`response_schema.required_values`.

If a pending request becomes stale because its bound source changed, or its
JSON/Markdown handoff no longer passes integrity checks, the old Review is
preserved as `SUPERSEDED` and a new bound request is issued. A legacy unbound
request is normalized to the same recoverable path during migration. If the
source changes after a repair, its required changes are reopened instead of
carrying the old remediation claims onto a different commit.

## Known local-runtime boundary

SQLite transactions make canonical state and Events atomic, but SQLite and
the filesystem cannot share one transaction. Staging and failure cleanup
close the ordinary rollback gap. V0.2 leases reclaim interrupted
`WORKSPACE_ONLY` execution and fence off late results; `EXTERNAL` work stops
for manual recovery. External or irreversible actions remain out of scope.

The built-in verifier is `SYNTHETIC_ONLY`. Its PASS result means only that the
artifact at the declared local path exactly matches fixed expected bytes. It
does not semantically evaluate the VentureContract metric, experiment, or
business pass/fail condition. Model-produced FAQ answers use the separate
rubric verifier. Contract-based validation for real Ventures remains out of
scope.

Context Manifests provide `LOGICAL_NAMESPACE_ONLY` organization, not a
security boundary. The coding executor adds a validated Git worktree and
Codex `workspace-write` sandbox, but confidential multi-tenant isolation is
still not claimed. Only a trusted local executor with synthetic, non-confidential
data is supported; real Ventures and untrusted executors are out of scope.

The V0.2 execution foundation claims a WorkOrder with a short `EXECUTING`
transition, commits that transaction, invokes the ExecutorPort without a
SQLite write lock, and then finalizes the Run in a second short transaction.
Handled executor failures restore the prior resumable status and append a
failure Event. Startup and `company reclaim-expired` recover expired claims,
while fence tokens reject any result arriving from the abandoned execution.
