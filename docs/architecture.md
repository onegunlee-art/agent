# V0.1 Implemented Architecture

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
version and marks the prior response `SUPERSEDED`.

## Component boundaries

- `roles.py`: the only three RoleSpecs and deterministic synthetic test data.
- `council.py`: pure role-field ownership and deterministic contract synthesis.
- `first_principles.py`: pure structural validation for FP_LITE, FP_STANDARD,
  and FP_FULL. FACT references must resolve to externally supplied trusted
  Evidence, source types use a positive allowlist, and policy supplies the
  minimum decision level. It does not determine real-world truth.
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
SHA-256, and canonical trusted Evidence IDs whose files are rehashed. An
unchanged retry is rejected. A passing
repair moves the WorkOrder to `AWAITING_REREVIEW`, records the resolutions as
SUBMITTED, and creates a new bound ReviewRequest. Only a reviewer PASS on that
new request marks the changes VERIFIED and the WorkOrder COMPLETED.

Each Review uses
`var/handoffs/reviews/<work_order_id>/<review_id>/`, so a later request or
response cannot overwrite prior audit material.

If a pending request becomes stale because its bound source changed, or its
JSON/Markdown handoff no longer passes integrity checks, the old Review is
preserved as `SUPERSEDED` and a new bound request is issued. A legacy unbound
request is normalized to the same recoverable path during migration. If the
source changes after a repair, its required changes are reopened instead of
carrying the old remediation claims onto a different commit.

## Known local-runtime boundary

SQLite transactions make canonical state and Events atomic, but SQLite and
the filesystem cannot share one transaction. Staging and failure cleanup
close the ordinary rollback gap, while a hard process interruption can still
leave a staging directory for later diagnosis. V0.1 ExecutorPorts are limited
to reversible local work. External or irreversible actions remain out of
scope.

The built-in verifier is `SYNTHETIC_ONLY`. Its PASS result means only that the
artifact at the declared local path exactly matches fixed expected bytes. It
does not semantically evaluate the VentureContract metric, experiment, or
business pass/fail condition. Contract-based verification for real Ventures
is not implemented in V0.1.

Context Manifests provide `LOGICAL_NAMESPACE_ONLY` organization, not a
security boundary. The runtime checks that the artifact returned by an
ExecutorPort is the declared path inside that Venture workspace, but it does
not sandbox the executor, restrict reads, or detect every write outside the
workspace. Only a trusted local executor with synthetic, non-confidential
data is supported; real Ventures and untrusted executors are out of scope.

The executor still runs inside the SQLite write transaction in this alpha.
Long-running or externally blocking executors are unsupported because they
can hold the local write lock. Decoupled execution claiming is deferred to a
later version.
