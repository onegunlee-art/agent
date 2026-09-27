from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Sequence

from .application import CompanyOS, ExistingArtifactExecutor
from .audit_events import (
    record_claude_verdict_archived,
    record_source_pushed,
)
from .dashboard import record_dashboard_action, serve_dashboard
from .customer_isolation import (
    backup_client_data,
    client_sparse_patterns,
    configure_customer_sparse_checkout,
    delete_client_data,
    initialize_client,
    load_client_policy,
    recover_client_deletion,
    restore_client_backup,
    save_isolated_evaluation_draft,
    sync_client_templates,
    validate_client_execution,
    verify_client_backup,
)
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
    capture_workspace_identity,
    create_worktree,
    validate_worktree,
)
from .roles import list_role_specs, serialize_role_spec
from .review_package import build_review_materials
from .skill_promotion import (
    approve_skill_candidate,
    candidate_tree_sha256,
    evaluate_skill_candidate,
    promote_skill_candidate,
)
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
    model_run.add_argument("--client-id")
    model_run.add_argument("--private-root", type=Path)

    review = commands.add_parser("review", help="Ingest a supplied ReviewResult")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_ingest = review_commands.add_parser("ingest")
    review_ingest.add_argument("review_id")
    review_ingest.add_argument("--file", required=True, type=Path)
    review_package = review_commands.add_parser("package")
    review_package.add_argument("--baseline", required=True)
    review_package.add_argument("--target", required=True)
    review_package.add_argument("--output-dir", required=True, type=Path)

    evaluation = commands.add_parser(
        "evaluation",
        help="Approve and run a hash-bound rubric evaluation",
    )
    evaluation_commands = evaluation.add_subparsers(
        dest="evaluation_command",
        required=True,
    )
    evaluation_approve = evaluation_commands.add_parser("approve")
    evaluation_approve.add_argument("work_order_id")
    evaluation_approve.add_argument("--cases", required=True, type=Path)
    evaluation_approve.add_argument("--expected-sha256", required=True)
    evaluation_approve.add_argument("--approval-file", required=True, type=Path)
    evaluation_approve.add_argument("--customer-id")
    evaluation_approve.add_argument("--idempotency-key", required=True)
    evaluation_run = evaluation_commands.add_parser("run")
    evaluation_run.add_argument("work_order_id")
    evaluation_run.add_argument("--run", required=True)
    evaluation_run.add_argument("--cases", required=True, type=Path)
    evaluation_run.add_argument("--data", required=True, type=Path)
    evaluation_run.add_argument("--approval", required=True)
    evaluation_run.add_argument("--idempotency-key", required=True)

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
    dashboard.add_argument("--backup-dir", type=Path)
    dashboard_action = commands.add_parser(
        "dashboard-action",
        help="Append one dashboard CEO action Event through the CLI",
    )
    dashboard_action_commands = dashboard_action.add_subparsers(
        dest="dashboard_action_command",
        required=True,
    )
    dashboard_approve = dashboard_action_commands.add_parser("approve")
    dashboard_approve.add_argument("work_order_id")
    dashboard_approve.add_argument("--idempotency-key", required=True)
    dashboard_change = dashboard_action_commands.add_parser("request-change")
    dashboard_change.add_argument("work_order_id")
    dashboard_change.add_argument("--request", required=True)
    dashboard_change.add_argument("--idempotency-key", required=True)

    skill = commands.add_parser(
        "skill", help="Evaluate, approve, and promote reusable skill candidates"
    )
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    skill_evaluate = skill_commands.add_parser("evaluate")
    skill_evaluate.add_argument("candidate", type=Path)
    skill_evaluate.add_argument("--test-arg", action="append", default=[])
    skill_evaluate.add_argument("--idempotency-key")
    skill_approve = skill_commands.add_parser("approve")
    skill_approve.add_argument("candidate", type=Path)
    skill_approve.add_argument("--evaluation-event")
    skill_approve.add_argument("--approval-file", type=Path)
    skill_approve.add_argument("--idempotency-key")
    skill_promote = skill_commands.add_parser("promote")
    skill_promote.add_argument("candidate", type=Path)
    skill_promote.add_argument("--approval-event")
    skill_promote.add_argument("--destination-root", type=Path)
    skill_promote.add_argument("--idempotency-key")

    client = commands.add_parser(
        "client", help="Manage isolated private customer workspaces"
    )
    client_commands = client.add_subparsers(dest="client_command", required=True)
    client_init = client_commands.add_parser("init")
    client_init.add_argument("client_id")
    client_init.add_argument("--private-root", type=Path, required=True)
    client_init.add_argument("--markers-file", type=Path, required=True)
    client_init.add_argument("--token-limit", type=int, required=True)
    client_init.add_argument("--cost-limit-usd", type=float, required=True)
    client_init.add_argument("--idempotency-key", required=True)
    client_policy = client_commands.add_parser("policy")
    client_policy.add_argument("client_id")
    client_policy.add_argument("--private-root", type=Path, required=True)
    client_evaluation = client_commands.add_parser("evaluation-draft")
    client_evaluation.add_argument("client_id")
    client_evaluation.add_argument("--private-root", type=Path, required=True)
    client_evaluation.add_argument("--intake", type=Path, required=True)
    client_evaluation.add_argument("--output", type=Path, required=True)
    client_backup = client_commands.add_parser("backup")
    client_backup.add_argument("client_id")
    client_backup.add_argument("--private-root", type=Path, required=True)
    client_backup.add_argument("--dir", type=Path, required=True)
    client_verify = client_commands.add_parser("verify-backup")
    client_verify.add_argument("--bundle", type=Path, required=True)
    client_restore = client_commands.add_parser("restore")
    client_restore.add_argument("--bundle", type=Path, required=True)
    client_restore.add_argument("--to-private-root", type=Path, required=True)
    client_delete = client_commands.add_parser("delete")
    client_delete.add_argument("client_id")
    client_delete.add_argument("--private-root", type=Path, required=True)
    client_delete.add_argument("--confirm-client-id", required=True)
    client_delete.add_argument("--idempotency-key", required=True)
    client_recover_delete = client_commands.add_parser("recover-delete")
    client_recover_delete.add_argument("client_id")
    client_recover_delete.add_argument("--private-root", type=Path, required=True)
    client_recover_delete.add_argument(
        "--deletion-idempotency-key", required=True
    )
    client_sync = client_commands.add_parser("sync-templates")
    client_sync.add_argument("client_id")
    client_sync.add_argument("--private-root", type=Path, required=True)
    client_sync.add_argument("--approval-file", type=Path, required=True)
    client_sync.add_argument("--idempotency-key", required=True)

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

    audit = commands.add_parser("audit", help="Record bounded reviewed audit events")
    audit_commands = audit.add_subparsers(dest="audit_command", required=True)
    audit_verdict = audit_commands.add_parser("archive-verdict")
    audit_verdict.add_argument("--file", type=Path, required=True)
    audit_verdict.add_argument("--expected-sha256", required=True)
    audit_verdict.add_argument(
        "--provenance",
        choices=(
            "ORIGINAL",
            "USER_SUPPLIED_TRANSCRIPT",
            "USER_SUPPLIED_TRANSCRIPT_SUMMARY",
        ),
        required=True,
    )
    audit_verdict.add_argument("--idempotency-key", required=True)
    audit_push = audit_commands.add_parser("source-pushed")
    audit_push.add_argument("--remote-url", required=True)
    audit_push.add_argument("--commit", required=True)
    audit_push.add_argument("--tag", action="append", required=True)
    audit_push.add_argument("--approval-file", type=Path, required=True)
    audit_push.add_argument("--idempotency-key", required=True)

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
        client_policy = None
        if bool(args.client_id) != bool(args.private_root):
            raise ValueError("customer model-run requires both --client-id and --private-root")
        if args.client_id:
            client_policy = load_client_policy(
                args.private_root, args.client_id, company=company
            )
            if client_policy.client_root not in instructions_path.parents:
                raise ValueError(
                    "customer instructions file must stay inside its private client folder"
                )
            validate_client_execution(
                client_policy,
                repository=repository,
                sparse_checkout_patterns=client_sparse_patterns(args.client_id),
                token_limit=work_order.token_limit,
                cost_limit_usd=work_order.cost_limit_usd,
            )
        if worktree.exists():
            if not args.reuse_worktree:
                raise ValueError(
                    "worktree already exists; pass --reuse-worktree only for an intentional repair"
                )
            workspace = validate_worktree(repository, args.branch, worktree)
        else:
            workspace = create_worktree(repository, args.branch, worktree)
        if client_policy is not None:
            if not args.reuse_worktree:
                configure_customer_sparse_checkout(
                    workspace, client_policy.sparse_checkout_patterns
                )
            identity = capture_workspace_identity(workspace)
            validate_client_execution(
                client_policy,
                repository=repository,
                sparse_checkout_patterns=identity.sparse_checkout_patterns,
                token_limit=work_order.token_limit,
                cost_limit_usd=work_order.cost_limit_usd,
            )
        test_command = tuple(args.test_arg)
        instructions = build_instructions(
            instructions_path.read_text(encoding="utf-8"),
            subprocess.list2cmdline(test_command),
        )
        run, outcome = company.execute_model_work_order(
            work_order.id,
            executor=CliCodingExecutor(),
            request=ExecutorRequest(
                work_order_id=work_order.id,
                workspace=workspace,
                instructions=instructions,
                time_limit_seconds=work_order.time_limit_seconds,
                cost_limit_usd=work_order.cost_limit_usd,
                model_call_limit=work_order.model_call_limit,
                token_limit=work_order.token_limit,
                test_command=test_command,
            ),
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
    if args.command == "review" and args.review_command == "package":
        return build_review_materials(
            company.root,
            baseline_commit=args.baseline,
            target_commit=args.target,
            output_dir=args.output_dir,
        )
    if args.command == "evaluation" and args.evaluation_command == "approve":
        approval_path = args.approval_file.resolve()
        if (
            approval_path.is_symlink()
            or not approval_path.is_file()
            or approval_path.stat().st_size > 64 * 1024
        ):
            raise ValueError("approval file must be a regular file of at most 64 KiB")
        return company.record_evaluation_approval(
            args.work_order_id,
            cases_path=args.cases.resolve(),
            expected_sha256=args.expected_sha256,
            approval_text=approval_path.read_text(encoding="utf-8"),
            customer_id=args.customer_id,
            idempotency_key=args.idempotency_key,
        )
    if args.command == "evaluation" and args.evaluation_command == "run":
        evidence = company.run_official_evaluation(
            args.work_order_id,
            args.run,
            cases_path=args.cases.resolve(),
            data_path=args.data.resolve(),
            approval_id=args.approval,
            idempotency_key=args.idempotency_key,
        )
        report = read_json(evidence.path)
        return {
            "status": "OFFICIAL_EVALUATION_RECORDED",
            "evidence": evidence,
            "verdict": report["verdict"],
            "score": report["score"],
            "threshold": report["threshold"],
            "case_count": report["case_count"],
            "official": report["official"],
            "evaluation_approval_id": report["evaluation_approval_id"],
            "evaluation_spec_sha256": report["evaluation_spec_sha256"],
            "source_commit": report["source_commit"],
            "source_tree_sha256": report["source_tree_sha256"],
        }
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
            backup_dir=args.backup_dir,
        )
        return {"status": "STOPPED"}
    if args.command == "dashboard-action":
        return record_dashboard_action(
            company,
            args.work_order_id,
            action=args.dashboard_action_command,
            request_text=(
                args.request
                if args.dashboard_action_command == "request-change"
                else ""
            ),
            idempotency_key=args.idempotency_key,
        )
    if args.command == "skill":
        candidate = args.candidate.resolve()
        candidate_hash = candidate_tree_sha256(candidate)
        if args.skill_command == "evaluate":
            manifest = read_json(candidate / "candidate.json")
            test_command = tuple(args.test_arg) or (
                sys.executable,
                "-m",
                "pytest",
                "-q",
                *[str(path) for path in manifest["required_evaluation_paths"]],
            )
            return evaluate_skill_candidate(
                company,
                candidate,
                test_command=test_command,
                idempotency_key=args.idempotency_key
                or f"cli-skill-evaluate:{candidate_hash}",
            )
        if args.skill_command == "approve":
            if not args.evaluation_event:
                raise ValueError("skill approve requires --evaluation-event")
            if args.approval_file is None:
                raise ValueError("skill approve requires --approval-file")
            approval_path = args.approval_file.resolve()
            if (
                approval_path.is_symlink()
                or not approval_path.is_file()
                or approval_path.stat().st_size > 64 * 1024
            ):
                raise ValueError("skill approval file must be regular and at most 64 KiB")
            return approve_skill_candidate(
                company,
                candidate,
                evaluation_event_id=args.evaluation_event,
                approval_text=approval_path.read_text(encoding="utf-8"),
                idempotency_key=args.idempotency_key
                or f"cli-skill-approve:{candidate_hash}:{args.evaluation_event}",
            )
        if not args.approval_event:
            raise ValueError("skill promote requires --approval-event")
        return promote_skill_candidate(
            company,
            candidate,
            approval_event_id=args.approval_event,
            destination_root=args.destination_root,
            idempotency_key=args.idempotency_key
            or f"cli-skill-promote:{candidate_hash}:{args.approval_event}",
        )
    if args.command == "client":
        if args.client_command == "init":
            markers = read_json(args.markers_file.resolve())
            if not isinstance(markers, dict):
                raise ValueError("markers file must contain one JSON object")
            return initialize_client(
                company,
                args.private_root,
                args.client_id,
                private_markers=markers.get("private_markers", []),
                customer_markers=markers.get("customer_markers", []),
                token_limit=args.token_limit,
                cost_limit_usd=args.cost_limit_usd,
                idempotency_key=args.idempotency_key,
            )
        if args.client_command == "policy":
            return load_client_policy(
                args.private_root, args.client_id, company=company
            )
        if args.client_command == "evaluation-draft":
            return save_isolated_evaluation_draft(
                args.private_root,
                args.client_id,
                args.intake,
                args.output,
                company=company,
            )
        if args.client_command == "backup":
            return backup_client_data(
                args.private_root,
                args.client_id,
                args.dir,
                company=company,
            )
        if args.client_command == "verify-backup":
            return verify_client_backup(args.bundle)
        if args.client_command == "restore":
            return {
                "status": "RESTORED",
                "client_root": restore_client_backup(
                    args.bundle, args.to_private_root
                ),
            }
        if args.client_command == "recover-delete":
            return recover_client_deletion(
                company,
                args.private_root,
                args.client_id,
                deletion_idempotency_key=args.deletion_idempotency_key,
            )
        if args.client_command == "sync-templates":
            return sync_client_templates(
                company,
                args.private_root,
                args.client_id,
                approval_file=args.approval_file,
                idempotency_key=args.idempotency_key,
            )
        return delete_client_data(
            company,
            args.private_root,
            args.client_id,
            confirmation=args.confirm_client_id,
            idempotency_key=args.idempotency_key,
        )
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
    if args.command == "audit" and args.audit_command == "archive-verdict":
        return record_claude_verdict_archived(
            company,
            args.file,
            expected_sha256=args.expected_sha256,
            provenance=args.provenance,
            idempotency_key=args.idempotency_key,
        )
    if args.command == "audit" and args.audit_command == "source-pushed":
        return record_source_pushed(
            company,
            remote_url=args.remote_url,
            commit=args.commit,
            tags=args.tag,
            approval_file=args.approval_file,
            idempotency_key=args.idempotency_key,
        )
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
