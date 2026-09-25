---
name: company-os-build
description: Implement or review one bounded AI Company OS repository change with executable acceptance tests, local durable state, and an auditable handoff. Use for changes to this repository's execution, ledger, review, handoff, or reusable-skill mechanisms.
---

# Company OS Build

Deliver one independently verifiable outcome. Treat the repository as the
company's operating kernel, not as the place for customer data or live
credentials.

## Establish the boundary

- Read the applicable `AGENTS.md`, current Git state, and the relevant tests
  before editing.
- Translate the request into one observable completion condition. Ask no more
  than three questions, and only when a missing answer would materially change
  the implementation.
- Work on a dedicated branch. Do not merge to `main` or push unless the CEO
  explicitly authorizes that action.
- Do not add a roadmap integration merely because it is available. Add one
  component only when the current acceptance test requires it.

## Build the change

- Write an executable failing acceptance test first. Keep fixtures synthetic
  and deterministic.
- Keep canonical decisions and current state in SQLite. Put larger evidence and
  handoff artifacts on the filesystem and bind them by hash.
- Keep external or potentially long execution outside SQLite write
  transactions. Record only short start, completion, and failure transitions.
- Preserve stop/resume, idempotency, context isolation, and append-only audit
  behavior when changing execution paths.
- Keep runtime state, logs, customer material, credentials, and generated
  handoffs out of tracked source.

## Prove completion

- Run the focused acceptance test, then the full suite with bytecode writes
  disabled when source binding matters.
- Inspect the final diff and Git status. Distinguish implemented behavior,
  verified behavior, remaining limitations, and deferred roadmap items.
- Report the exact user entry point and command for any new workflow. Never
  claim an external model review or real-business validation unless it occurred.
