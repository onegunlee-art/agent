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
  -> isolated Venture + Context Manifest
  -> first WorkOrder + immutable verifier hash
  -> ExecutorPort
  -> deterministic verification
  -> Evidence + append-only Event
  -> ReviewRequest JSON/Markdown
  -> WAITING_FOR_OPUS
  -> restart and resume from SQLite
```

The compiler requires all three role responses. It checks that their explicit
contract contributions are structurally identical; agreement is recorded as
provenance and is never promoted to Evidence. A conflict creates an Inbox
artifact and stops compilation for a CEO decision.

## Component boundaries

- `roles.py`: the only three RoleSpecs and deterministic synthetic test data.
- `first_principles.py`: pure structural validation for FP_LITE, FP_STANDARD,
  and FP_FULL. It does not determine real-world truth.
- `storage.py`: SQLite schema, transactions, idempotency, global stop state,
  immutable Event ledger, and JSONL export.
- `application.py`: state transitions, Venture scaffolding, isolation,
  handoffs, execution, verification, Evidence, review, and resume logic.
- `verifier.py`: built-in exact-text verifier and SHA-256 integrity helpers.
- `cli.py`: Windows-friendly `argparse` interface.
- `fakes.py`: deterministic test doubles only; never represented as real
  executive or Opus execution.

## Canonical state

SQLite owns Ideas, council responses, Contracts, Ventures, Assumptions,
Metrics, Experiments, WorkOrders, Runs, Reviews, Evidence metadata,
Decisions, Approvals, Artifact metadata, Events, global stop state, and
idempotency state. Every state transition that emits an Event writes both in
one transaction.

The filesystem owns actual artifacts, Context Manifests, council and review
handoffs, and verifier output. Writes use a sibling temporary file followed
by atomic replacement. SQLite paths are stored relative to the selected root
and resolved with containment checks.

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
- Review preparation rehashes Evidence and Artifact files. Request payload and
  file hashes are checked again when a ReviewResult is ingested.

## Review boundary

A real run creates an Opus ReviewRequest and stops in `WAITING_FOR_OPUS`.
Only a user-supplied response can continue that real workflow. Automated
tests use FakeReviewer, exercise one repair, and re-run the deterministic
verifier without claiming Claude performed the review.

Each Review uses
`var/handoffs/reviews/<work_order_id>/<review_id>/`, so a later request or
response cannot overwrite prior audit material.

## Known local-runtime boundary

SQLite transactions make canonical state and Events atomic, but SQLite and
the filesystem cannot share one transaction. A process interruption can
leave an unreferenced local temporary or artifact file; restart trusts only
committed SQLite state, and deterministic writes replace the expected path.
V0.1 ExecutorPorts are therefore limited to reversible local work. External
or irreversible actions remain out of scope.
