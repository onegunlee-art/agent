from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Sequence

from .application import CompanyOS, ExistingArtifactExecutor
from .errors import CompanyOSError
from .roles import list_role_specs, serialize_role_spec
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
        description="Local, resumable AI Company OS V0.1",
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

    review = commands.add_parser("review", help="Ingest a supplied ReviewResult")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_ingest = review_commands.add_parser("ingest")
    review_ingest.add_argument("review_id")
    review_ingest.add_argument("--file", required=True, type=Path)

    commands.add_parser("stop", help="Persistently disable new execution")
    commands.add_parser("resume", help="Re-enable execution")
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
        fingerprint = payload_hash(
            {
                "idea_id": args.idea_id,
                "min_level": args.min_level,
                "responses": [dict(row) for row in active],
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
        key = args.idempotency_key or (
            f"cli-resolve:{args.idea_id}:{contract_fingerprint}:{args.min_level}"
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
