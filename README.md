# AI Company OS V0.1

AI Company OS is a local, on-demand operating kernel that turns a one-line
idea into an isolated Venture, a mechanically verified WorkOrder, durable
Evidence, and an auditable Decision trail.

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
company council prepare <idea_id>
company council ingest <idea_id> --role cto --file <cto_response.json>
company council ingest <idea_id> --role cpo --file <cpo_response.json>
company council ingest <idea_id> --role cmo --file <cmo_response.json>
company council compile <idea_id>
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
company review ingest <review_id> --file <review_result.json>
company events export --output <events.jsonl>
```

Use `--root <path>` on any command to select a Company OS root. Canonical
state defaults to `var/state/company.db` below that root.

## State and artifacts

- SQLite is the only canonical current-state store.
- The Event table is append-only; JSONL is an explicit audit export.
- Venture workspaces, handoffs, Evidence files, logs, and runtime databases
  live under `var/` and are excluded from Git.
- Every filesystem path stored in SQLite is relative to the Company OS root.
- Each Venture has its own workspace and Context Manifest.
- The verifier definition is hashed before execution. A changed hash marks
  the Run `INVALIDATED` and records `VERIFIER_TAMPER_DETECTED`.
- Before a ReviewRequest is created, Evidence and Artifact files are rehashed
  against SQLite. Each review has a unique, immutable handoff directory.

See [architecture](docs/architecture.md) for the implemented flow and
invariants, and [CLAUDE.md](CLAUDE.md) for manual review handoff rules.

## V0.1 boundaries

V0.1 has exactly three C-level RoleSpecs: CTO, CPO, and CMO. It does not
include a web dashboard, scheduler, daemon, external database or queue,
vector database, web crawler, external messaging, production deployment, or
real customer data. All automated tests and the bootstrap smoke test use
synthetic data.
