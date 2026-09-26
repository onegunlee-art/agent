from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Sequence

from .application import CompanyOS, ExistingArtifactExecutor
from .dashboard import serve_dashboard
from .errors import CompanyOSError
from .ledger_backup import (
    backup,
    backup_recovery_bundle,
    restore,
    restore_recovery_bundle,
    verify,
    verify_recovery_bundle,
)
from .model_executor import (
    CliCodingExecutor,
    ExecutorRequest,
    build_instructions,
    create_worktree,
    validate_worktree,
)
from .roles import list_role_specs, serialize_role_spec
from .synthetic_faq import serve as serve_synthetic_faq
from .utils import payload_hash, read_json, sha256_file


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=_json_default))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="company",
        description="Local, resumable AI Company OS V0.2",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Initialize local canonical state")

    idea = commands.add_parser("idea", help="Manage one-line ideas")
    idea_commands = idea.add_subparsers(dest="idea_command", required=True)
    idea_create = idea_commands.add_parser("create")
    idea_create.add_argument("text")
    idea_create.add_argument("--idempotency-key")

    evidence = commands.add_parser("evidence", help="Register trusted local Evidence")
    evidence_commands = evidence.add_subparsers(
        dest="evidence_command",
        required=True,
    )
    evidence_add = evidence_commands.add_parser("add")
    evidence_add.add_argument("--idea", required=True)
    evidence_add.add_argument("--file", required=True, type=Path)
    evidence_add.add_argument("--external-ref")
    evidence_add.add_argument("--idempotency-key")
    evidence_test = evidence_commands.add_parser("add-test-result")
    evidence_test.add_argument("work_order_id")
    evidence_test.add_argument("--review")
    evidence_test.add_argument("--change", required=True)
    evidence_test.add_argument("--file", required=True, type=Path)
    evidence_test.add_argument("--node-id", required=True, action="append")
    evidence_test.add_argument("--source-commit", required=True)
    evidence_test.add_argument("--idempotency-key")

    council = commands.add_parser("council", help="Manual executive handoffs")
    council_commands = council.add_subparsers(dest="council_command", required=True)
    council_prepare = council_commands.add_parser("prepare")
    council_prepare.add_argument("idea_id")
    council_ingest = council_commands.add_parser("ingest")
    council_ingest.add_argument("idea_id")
    council_ingest.add_argument("--role", required=True, choices=("cto", "cpo", "cmo"))
    council_ingest.add_argument("--file", required=True, type=Path)
    council_compile = council_commands.add_parser("compile")
    council_compile.add_argument("idea_id")
    council_compile.add_argument(
        "--min-level",
        default="FP_STANDARD",
        choices=("FP_LITE", "FP_STANDARD", "FP_FULL"),
    )
    council_compile.add_argument("--idempotency-key")
    council_resolve = council_commands.add_parser("resolve")
    council_resolve.add_argument("idea_id")
    council_resolve.add_argument("--contract-file", required=True, type=Path)
    council_resolve.add_argument(
        "--min-level",
        default="FP_STANDARD",
        choices=("FP_LITE", "FP_STANDARD", "FP_FULL"),
    )
    council_resolve.add_argument("--idempotency-key")

    roles = commands.add_parser("roles", help="Inspect the three RoleSpecs")
    roles_commands = roles.add_subparsers(dest="roles_command", required=True)
    roles_commands.add_parser("list")
    role_show = roles_commands.add_parser("show")
    role_show.add_argument("role", choices=("cto", "cpo", "cmo"))

    venture = commands.add_parser(
        "venture", help="Create a venture-scoped local workspace"
    )
    venture_commands = venture.add_subparsers(dest="venture_command", required=True)
    scaffold = venture_commands.add_parser("scaffold")
    scaffold.add_argument("contract_id")
    scaffold.add_argument(
        "--approval-status",
        default="NOT_REQUIRED",
        choices=("APPROVED", "NOT_REQUIRED"),
    )
    scaffold.add_argument("--idempotency-key")

    work = commands.add_parser("work", help="Execute, verify, and resume WorkOrders")
    work_commands = work.add_subparsers(dest="work_command", required=True)
    work_status = work_commands.add_parser("status")
    work_status.add_argument("work_order_id")
    work_verify = work_commands.add_parser("verify")
    work_verify.add_argument("work_order_id")
    work_verify.add_argument("--idempotency-key")
    work_review = work_commands.add_parser("review")
    work_review.add_argument("work_order_id")
    work_review.add_argument("--idempotency-key")
    work_resume = work_commands.add_parser("resume")
    work_resume.add_argument("work_order_id")
    work_resume.add_argument("--repair-manifest", type=Path)
    model_run = work_commands.add_parser(
        "model-run",
        help="Run one bounded coding-agent CLI in a Git worktree",
    )
    model_run.add_argument("work_order_id")
    model_run.add_argument("--repository", required=True, type=Path)
    model_run.add_argument("--worktree", required=True, type=Path)
    model_run.add_argument("--branch", required=True)
    model_run.add_argument("--instructions-file", required=True, type=Path)
    model_run.add_argument("--test-arg", required=True, action="append")
    model_run.add_argument("--reuse-worktree", action="store_true")
    model_run.add_argument("--idempotency-key", required=True)

    review = commands.add_parser("review", help="Ingest a supplied ReviewResult")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_ingest = review_commands.add_parser("ingest")
    review_ingest.add_argument("review_id")
    review_ingest.add_argument("--file", required=True, type=Path)

    commands.add_parser("stop", help="Persistently disable new execution")
    commands.add_parser("resume", help="Re-enable execution")
    commands.add_parser("reclaim-expired", help="Reclaim expired execution leases")
    preview = commands.add_parser("preview", help="Serve the synthetic chatbot locally")
    preview.add_argument("--data", type=Path)
    preview.add_argument("--port", type=int, default=8765)
    dashboard = commands.add_parser("dashboard", help="Serve the local work dashboard")
    dashboard.add_argument("--port", type=int, default=8780)
    dashboard.add_argument(
        "--preview-url", default="http://127.0.0.1:8765/"
    )

    ledger = commands.add_parser("ledger", help="Back up, verify, or restore the ledger")
    ledger_commands = ledger.add_subparsers(dest="ledger_command", required=True)
    ledger_backup = ledger_commands.add_parser("backup")
    ledger_backup.add_argument("--dir", type=Path, required=True)
    ledger_verify = ledger_commands.add_parser("verify")
    ledger_verify.add_argument("--backup", type=Path, required=True)
    ledger_restore = ledger_commands.add_parser("restore")
    ledger_restore.add_argument("--backup", type=Path, required=True)
    ledger_restore.add_argument("--to", type=Path, required=True)
    recovery_backup = ledger_commands.add_parser("recovery-backup")
    recovery_backup.add_argument("--dir", type=Path, required=True)
    recovery_verify = ledger_commands.add_parser("recovery-verify")
    recovery_verify.add_argument("--bundle", type=Path, required=True)
    recovery_restore = ledger_commands.add_parser("recovery-restore")
    recovery_restore.add_argument("--bundle", type=Path, required=True)
    recovery_restore.add_argument("--to-db", type=Path, required=True)
    recovery_restore.add_argument("--to-root", type=Path, required=True)
    commands.add_parser("status", help="Show durable company state")
    commands.add_parser("inbox", help="Show pending Decision and Approval items")

    events = commands.add_parser("events", help="Export the append-only ledger")
    event_commands = events.add_subparsers(dest="events_command", required=True)
    event_export = event_commands.add_parser("export")
    event_export.add_argument("--output", required=True, type=Path)
    return parser


def _dispatch(company: CompanyOS, args: argparse.Namespace) -> Any:
    if args.command == "init":
        return {
            "status": "INITIALIZED",
            "root": company.root,
            "db_path": company.db_path,
            "journal_mode": company.store.journal_mode(),
        }
    if args.command == "idea" and args.idea_command == "create":
        key = args.idempotency_key or f"cli-idea:{payload_hash({'text': args.text})}"
        return company.create_idea(args.text, idempotency_key=key)
    if args.command == "evidence" and args.evidence_command == "add":
        file_digest = (
            sha256_file(args.file.resolve()) if args.file.is_file() else "MISSING"
        )
        key = args.idempotency_key or (
            f"cli-idea-evidence:{args.idea}:"
            f"{payload_hash({'sha256': file_digest, 'external_ref': args.external_ref})}"
        )
        return company.register_idea_evidence(
            args.idea,
            evidence_file=args.file,
            external_ref=args.external_ref,
            idempotency_key=key,
        )
    if args.command == "evidence" and args.evidence_command == "add-test-result":
        file_digest = (
            sha256_file(args.file.resolve()) if args.file.is_file() else "MISSING"
        )
        key = args.idempotency_key or (
            f"cli-test-result:{args.work_order_id}:{args.change}:"
            f"{payload_hash({'sha256': file_digest, 'nodes': sorted(args.node_id), 'review': args.review, 'source_commit': args.source_commit})}"
        )
        return company.register_test_result(
            args.work_order_id,
            required_change_id=args.change,
            result_file=args.file,
            test_node_ids=args.node_id,
            review_id=args.review,
            source_commit=args.source_commit,
            idempotency_key=key,
        )
    if args.command == "council" and args.council_command == "prepare":
        return company.prepare_council(args.idea_id)
    if args.command == "council" and args.council_command == "ingest":
        path = company.ingest_council_response(
            args.idea_id,
            role=args.role,
            response_file=args.file,
        )
        return {"status": "INGESTED", "path": path}
    if args.command == "council" and args.council_command == "compile":
        active = company.store.query_all(
            """
            SELECT role, response_hash FROM council_responses
            WHERE idea_id = ? AND status = 'ACTIVE' ORDER BY role
            """,
            (args.idea_id,),
        )
        idea_row = company.store.get_row("ideas", args.idea_id)
        fingerprint = payload_hash(
            {
                "idea_id": args.idea_id,
                "min_level": args.min_level,
                "responses": [dict(row) for row in active],
                "evidence_grants": company.idea_evidence_fingerprint(args.idea_id),
                "idea_status": idea_row["status"] if idea_row is not None else None,
            }
        )
        key = args.idempotency_key or f"cli-compile:{args.idea_id}:{fingerprint}"
        outcome = company.compile_council(
            args.idea_id,
            min_decision_level=args.min_level,
            idempotency_key=key,
        )
        return {
            "contract_id": outcome.contract_id,
            "gate": {
                "passed": outcome.gate_result.passed,
                "violations": [asdict(v) for v in outcome.gate_result.violations],
            },
        }
    if args.command == "council" and args.council_command == "resolve":
        contract_fingerprint = sha256_file(args.contract_file.resolve())
        idea_row = company.store.get_row("ideas", args.idea_id)
        resolution_context = payload_hash(
            {
                "contract": contract_fingerprint,
                "min_level": args.min_level,
                "evidence_grants": company.idea_evidence_fingerprint(args.idea_id),
                "idea_status": idea_row["status"] if idea_row is not None else None,
            }
        )
        key = args.idempotency_key or (
            f"cli-resolve:{args.idea_id}:{resolution_context}"
        )
        outcome = company.resolve_council(
            args.idea_id,
            contract_file=args.contract_file,
            min_decision_level=args.min_level,
            idempotency_key=key,
        )
        return {
            "contract_id": outcome.contract_id,
            "gate": {
                "passed": outcome.gate_result.passed,
                "violations": [asdict(v) for v in outcome.gate_result.violations],
            },
        }
    if args.command == "roles" and args.roles_command == "list":
        return {"roles": [name for name, _ in list_role_specs()]}
    if args.command == "roles" and args.roles_command == "show":
        return serialize_role_spec(args.role)
    if args.command == "venture" and args.venture_command == "scaffold":
        key = args.idempotency_key or (
            f"cli-scaffold:{args.contract_id}:{args.approval_status}"
        )
        return company.record_approval_and_scaffold(
            args.contract_id,
            approval_status=args.approval_status,
            idempotency_key=key,
        )
    if args.command == "work" and args.work_command == "status":
        return company.work_order(args.work_order_id)
    if args.command == "work" and args.work_command == "model-run":
        prior = company.store.query_one(
            "SELECT status FROM idempotency WHERE key = ?",
            (args.idempotency_key,),
        )
        if prior is not None:
            raise ValueError(
                "model-run idempotency key already exists; refusing a repeated model call"
            )
        instructions_path = args.instructions_file.resolve()
        if (
            instructions_path.is_symlink()
            or not instructions_path.is_file()
            or instructions_path.stat().st_size > 64 * 1024
        ):
            raise ValueError("instructions file must be a regular file of at most 64 KiB")
        work_order = company.work_order(args.work_order_id)
        repository = args.repository.resolve()
        worktree = args.worktree.resolve()
        if worktree.exists():
            if not args.reuse_worktree:
                raise ValueError(
                    "worktree already exists; pass --reuse-worktree only for an intentional repair"
                )
            workspace = validate_worktree(repository, args.branch, worktree)
        else:
            workspace = create_worktree(repository, args.branch, worktree)
        test_command = tuple(args.test_arg)
        instructions = build_instructions(
            instructions_path.read_text(encoding="utf-8"),
            subprocess.list2cmdline(test_command),
        )
        outcome = CliCodingExecutor().run(
            ExecutorRequest(
                work_order_id=work_order.id,
                workspace=workspace,
                instructions=instructions,
                time_limit_seconds=work_order.time_limit_seconds,
                cost_limit_usd=work_order.cost_limit_usd,
                model_call_limit=work_order.model_call_limit,
                token_limit=work_order.token_limit,
                test_command=test_command,
            )
        )
        run = company.record_model_execution(
            work_order.id,
            outcome,
            idempotency_key=args.idempotency_key,
        )
        reproducibility = company.record_run_reproducibility(
            run.id,
            instructions=instructions,
            test_command=list(test_command),
            workspace_branch=outcome.workspace_branch,
            workspace_head=outcome.workspace_head,
            sparse_checkout_patterns=outcome.sparse_checkout_patterns,
            workspace_diff=outcome.workspace_diff,
            changed_file_sha256=outcome.changed_file_sha256,
            provenance="CAPTURED_AT_EXECUTION",
            notes="captured immediately after the coding-agent process and acceptance command",
            idempotency_key=f"{args.idempotency_key}:reproducibility",
        )
        return {
            "run": run,
            "reproducibility_evidence_id": reproducibility.id,
            "executor_outcome": outcome.label,
            "executor_ok": outcome.ok,
            "cost_status": run.payload.get("cost_status"),
            "usage_status": run.payload.get("usage_status"),
            "budget_basis": run.payload.get("budget_basis"),
            "changed_files": outcome.changed_files,
            "usage": outcome.usage,
            "worktree": workspace,
        }
    if args.command == "work" and args.work_command == "verify":
        work_order = company.work_order(args.work_order_id)
        workspace = company.venture(work_order.venture_id).workspace_path
        artifact_path = workspace / work_order.artifact_relative_path
        artifact_fingerprint = (
            sha256_file(artifact_path) if artifact_path.is_file() else "MISSING"
        )
        key = args.idempotency_key or (
            f"cli-verify:{args.work_order_id}:"
            f"{payload_hash({'artifact_sha256': artifact_fingerprint})}"
        )
        return company.verify_existing_artifact(
            args.work_order_id, idempotency_key=key
        )
    if args.command == "work" and args.work_command == "review":
        review_count = int(
            company.store.scalar(
                "SELECT COUNT(*) FROM reviews WHERE work_order_id = ?",
                (args.work_order_id,),
            )
        )
        key = args.idempotency_key or (
            f"cli-review:{args.work_order_id}:{review_count}"
        )
        return company.prepare_review(args.work_order_id, idempotency_key=key)
    if args.command == "work" and args.work_command == "resume":
        repair_manifest = None
        if args.repair_manifest is not None:
            repair_manifest = read_json(args.repair_manifest.resolve())
            if not isinstance(repair_manifest, dict):
                raise ValueError("repair manifest must be a JSON object")
        return company.resume_work_order(
            args.work_order_id,
            executor=ExistingArtifactExecutor(),
            repair_manifest=repair_manifest,
        )
    if args.command == "review" and args.review_command == "ingest":
        return company.ingest_review_result(args.review_id, args.file)
    if args.command == "stop":
        company.stop()
        return {"stopped": True}
    if args.command == "resume":
        company.resume()
        return {"stopped": False}
    if args.command == "reclaim-expired":
        reclaimed = company.reclaim_expired()
        return {"status": "RECLAIMED", "count": len(reclaimed), "items": reclaimed}
    if args.command == "preview":
        data_path = args.data or (
            company.root / "examples" / "synthetic-cafe-a" / "faq_data.json"
        )
        serve_synthetic_faq(data_path, port=args.port)
        return {"status": "STOPPED"}
    if args.command == "dashboard":
        serve_dashboard(
            company.root,
            company.db_path,
            port=args.port,
            preview_url=args.preview_url,
        )
        return {"status": "STOPPED"}
    if args.command == "ledger" and args.ledger_command == "backup":
        return backup(company.db_path, args.dir)
    if args.command == "ledger" and args.ledger_command == "verify":
        return verify(args.backup)
    if args.command == "ledger" and args.ledger_command == "restore":
        if args.to.resolve() == company.db_path.resolve():
            raise ValueError("restore target must be separate from the live ledger")
        return {"status": "RESTORED", "table_counts": restore(args.backup, args.to)}
    if args.command == "ledger" and args.ledger_command == "recovery-backup":
        return backup_recovery_bundle(company.db_path, company.root, args.dir)
    if args.command == "ledger" and args.ledger_command == "recovery-verify":
        return verify_recovery_bundle(args.bundle)
    if args.command == "ledger" and args.ledger_command == "recovery-restore":
        if args.to_db.resolve() == company.db_path.resolve():
            raise ValueError("restore database must be separate from the live ledger")
        if args.to_root.resolve() == company.root.resolve():
            raise ValueError("restore root must be separate from the live company root")
        restored = restore_recovery_bundle(
            args.bundle,
            new_db_path=args.to_db,
            new_root=args.to_root,
        )
        return {"status": "RESTORED", "recovery": restored}
    if args.command == "status":
        waiting = company.store.query_all(
            "SELECT id, work_order_id, binding_status, request_json_path, "
            "request_markdown_path "
            "FROM reviews WHERE status = 'WAITING_FOR_OPUS' ORDER BY created_at"
        )
        legacy_unbound = company.store.query_all(
            "SELECT id, work_order_id, status FROM reviews "
            "WHERE binding_status = 'LEGACY_UNBOUND' ORDER BY created_at"
        )
        return {
            "root": company.root,
            "db_path": company.db_path,
            "stopped": company.is_stopped(),
            "waiting_for_opus": [dict(row) for row in waiting],
            "legacy_unbound_reviews": [dict(row) for row in legacy_unbound],
            "event_count": len(company.events()),
        }
    if args.command == "inbox":
        return company.inbox()
    if args.command == "events" and args.events_command == "export":
        path = company.export_event_ledger(args.output)
        return {"status": "EXPORTED", "path": path}
    raise AssertionError("Unhandled CLI command")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    company = CompanyOS(root=args.root, db_path=args.db)
    try:
        company.initialize()
        _print(_dispatch(company, args))
        return 0
    except (CompanyOSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2
    finally:
        company.close()


if __name__ == "__main__":
    raise SystemExit(main())
