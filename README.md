# AI Company OS V0.2

Status: **V0.2 implementation branch**. A real Codex CLI call has edited a
synthetic chatbot worktree and passed its acceptance test. V0.2 is not final
until the CEO approves the evaluation cases and an independent Claude review
returns PASS.

AI Company OS is a local, on-demand operating kernel that turns a one-line
idea into a venture-scoped workspace, a mechanically checked WorkOrder,
durable Evidence, and an auditable Decision trail.

The operating kernel remains an on-demand Python CLI. V0.2 adds an explicitly
invoked Codex CLI executor, rubric evaluation, a deterministic synthetic FAQ
bot, a loopback-only status page, execution leases, and online ledger backup.
CTO, CPO, CMO, and Claude interactions still use structured file handoffs.

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

Use `--root <path>` on any command to select a Company OS root. For a Git
repository, canonical state defaults to
`%LOCALAPPDATA%\ai-company-os\ledger.sqlite3`, outside the repository and
OneDrive. A legacy `var/state/company.db` is migrated online and preserved.

## V0.2 execution and browser entry points

Create a UTF-8 instruction file in the editor, then run one coding agent in a
new Git worktree. Each `--test-arg` is one subprocess argument.

```powershell
company --root C:\dev\ai-company-os work model-run <work_order_id> `
  --repository C:\path\to\synthetic-repo `
  --worktree C:\dev\ai-company-os\var\worktrees\<work_order_id> `
  --branch wo/<work_order_id> `
  --instructions-file C:\path\to\work-order.txt `
  --test-arg C:\dev\ai-company-os\.venv\Scripts\python.exe `
  --test-arg acceptance_test.py `
  --idempotency-key <unique-key>
```

The WorkOrder supplies the time, model-call, token, and USD limits. ChatGPT
login runs may not expose per-run USD: this is recorded as
`cost_status=UNAVAILABLE`, while the independently measurable call, token, and
time limits remain enforced. A known dollar overrun is recorded separately as
`COST_LIMIT_EXCEEDED`.

Local browser surfaces bind only to `127.0.0.1`:

```powershell
company --root C:\dev\ai-company-os preview --port 8765
company --root C:\dev\ai-company-os dashboard --port 8780
```

Open `http://127.0.0.1:8765/` for the chatbot and
`http://127.0.0.1:8780/` for the work dashboard.

Back up and verify the external ledger without copying a live SQLite file:

```powershell
company --root C:\dev\ai-company-os ledger backup --dir C:\safe-backups
company --root C:\dev\ai-company-os ledger verify --backup <backup.sqlite3>
company --root C:\dev\ai-company-os ledger restore --backup <backup.sqlite3> --to <new-ledger.sqlite3>
```

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
invariants, [개인 에이전트 코딩 업무 환경](docs/OPERATING_GUIDE_KO.md) for the
exact conversation and operating entry points, [V0.2 방향과 구현 순서](docs/ROADMAP_V0.2_KO.md)
for the approved build sequence, and [CLAUDE.md](CLAUDE.md) for manual review
handoff rules.

## V0.2 boundaries

The OS still has exactly three C-level RoleSpecs: CTO, CPO, and CMO. It does
not include a scheduler, daemon, external queue, vector database, web crawler,
external messaging, payments, production deployment, or real customer data.
All automated tests and the first model cycle use synthetic data.

The built-in exact-text verifier remains `SYNTHETIC_ONLY`. A PASS proves only that
the declared local path contains the expected bytes. It does not evaluate a
VentureContract's metric, experiment, or real-world pass/fail outcome, so V0.2
must not be used to validate a real Venture.

The Context Manifest provides `LOGICAL_NAMESPACE_ONLY` organization. The
coding executor therefore also uses a Git worktree plus Codex
`workspace-write` sandboxing. Worktrees are isolation aids, not security
boundaries. Confidential multi-tenant workloads remain out of scope.
