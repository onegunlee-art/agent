# AI Company OS V0.1

Status: **V0.1-alpha**. This repository proves the synthetic local workflow;
it is not ready for a real Venture.

AI Company OS is a local, on-demand operating kernel that turns a one-line
idea into a venture-scoped workspace, a mechanically checked WorkOrder,
durable Evidence, and an auditable Decision trail.

V0.1 is intentionally a Python CLI, not a chatbot or background service. It
does not call the OpenAI or Anthropic APIs and does not invoke Codex
recursively. CTO, CPO, CMO, and Claude Opus interactions use structured JSON
and Markdown file handoffs.

## Requirements

- Windows, macOS, or Linux with Python 3.11+
- Git
- `pytest` for development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

## Minimal CLI flow

```powershell
company init
company idea create "Create one synthetic artifact and verify it."
company evidence add --idea <idea_id> --file <source_document> --external-ref <ref>
company council prepare <idea_id>
company council ingest <idea_id> --role cto --file <cto_response.json>
company council ingest <idea_id> --role cpo --file <cpo_response.json>
company council ingest <idea_id> --role cmo --file <cmo_response.json>
company council compile <idea_id> --min-level FP_STANDARD
company venture scaffold <contract_id> --approval-status NOT_REQUIRED
company work verify <work_order_id>
company work review <work_order_id>
company status
```

`company council prepare` creates the exact request files under
`var/handoffs/council/<idea_id>/`. The three responses are produced outside
the program and ingested explicitly. `company work verify` verifies an
artifact already created by the current external executor in the Venture
workspace. A real run stops normally at `WAITING_FOR_OPUS` and prints the
ReviewRequest paths.

Durable operational commands:

```powershell
company stop
company resume
company inbox
company work resume <work_order_id>
company work resume <work_order_id> --repair-manifest <repair_manifest.json>
company review ingest <review_id> --file <review_result.json>
company events export --output <events.jsonl>
```

Use `--root <path>` on any command to select a Company OS root. Canonical
state defaults to `var/state/company.db` below that root.

Corrected executive responses can be ingested again; the prior version is
retained as `SUPERSEDED`. If a genuinely shared Council field conflicts, use
`company council resolve <idea_id> --contract-file <complete_contract.json>`.
Non-owner opinions are retained as hash-only Council provenance; disagreements
with the field owner's value are also shown in `company inbox` without
silently replacing the owner value.

## State and artifacts

- SQLite is the only canonical current-state store.
- The Event table is append-only; JSONL is an explicit audit export.
- Venture workspaces, handoffs, Evidence files, logs, and runtime databases
  live under `var/` and are excluded from Git.
- Every filesystem path stored in SQLite is relative to the Company OS root.
- Each Venture has its own workspace and Context Manifest. This is logical
  namespacing, not a process or filesystem security boundary.
- The verifier definition is hashed before execution. A changed hash marks
  the Run `INVALIDATED` and records `VERIFIER_TAMPER_DETECTED`.
- Before a ReviewRequest is created, Evidence and Artifact files are rehashed
  against SQLite. Each review has a unique, immutable handoff directory.
- Schema-v2 ReviewRequests contain the reviewable WorkOrder inputs and bind
  the decision to a Git commit plus deterministic tracked-tree SHA-256.
- Review and repair binding requires a clean Git source state; untracked
  source, project-local `__pycache__`/`.egg-info`, symlinks, gitlinks, and
  unapproved ignored paths are rejected. Run source-binding commands without
  writing bytecode (for example, set `PYTHONDONTWRITEBYTECODE=1`) after removing
  generated project caches. The interpreter and installed dependencies are a
  trusted environment boundary, not part of the source digest.
- Both the JSON and Markdown ReviewRequest files are hash-bound and rechecked
  before a ReviewResult can change state.
- A repair requires one hash-checked resolution per required-change ID and
  a change-specific `TEST_RESULT` Evidence record bound to test node IDs and
  the current source commit. Register it with
  `company evidence add-test-result <work_order_id> --review <review_id>
  --change <change_id> --file <test_result> --node-id <pytest_node_id>
  --source-commit <commit>`.
  Every repair always returns to independent rereview before completion.
- The automatic synthetic fixture can support only its exact fixture-exists
  statement. Other FACTs require an explicitly registered, hash-bound CEO
  document; registration records a claimed actor but does not authenticate a
  human identity.
- A stale, legacy, or damaged pending review is preserved as `SUPERSEDED` and
  reissued against current clean source; it cannot strand the WorkOrder in an
  unrecoverable `WAITING_FOR_OPUS` state.

See [architecture](docs/architecture.md) for the implemented flow and
invariants, and [CLAUDE.md](CLAUDE.md) for manual review handoff rules.

## V0.1 boundaries

V0.1 has exactly three C-level RoleSpecs: CTO, CPO, and CMO. It does not
include a web dashboard, scheduler, daemon, external database or queue,
vector database, web crawler, external messaging, production deployment, or
real customer data. All automated tests and the bootstrap smoke test use
synthetic data.

The built-in exact-text verifier is `SYNTHETIC_ONLY`. A PASS proves only that
the declared local path contains the expected bytes. It does not evaluate a
VentureContract's metric, experiment, or real-world pass/fail outcome, so V0.1
must not be used to validate a real Venture.

The Context Manifest provides `LOGICAL_NAMESPACE_ONLY` organization. It is
not a sandbox: a local executor may still read the repository or modify files
outside its Venture workspace. V0.1 therefore permits only a trusted local
executor operating on synthetic, non-confidential data; untrusted executors
and confidential multi-Venture workloads are out of scope.
