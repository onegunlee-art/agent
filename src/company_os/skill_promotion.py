"""Auditable candidate evaluation, CEO approval, and atomic skill promotion."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Sequence

from .application import CompanyOS
from .errors import ConflictError, ValidationError
from .storage import IdempotencyConflict
from .utils import (
    atomic_write_json,
    canonical_json,
    new_id,
    sha256_file,
    utc_now,
)


Runner = Callable[..., subprocess.CompletedProcess[str]]
_CANDIDATE_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")


def _manifest(candidate_dir: str | Path) -> tuple[Path, dict[str, Any]]:
    root = Path(candidate_dir).resolve()
    source = root / "candidate.json"
    if root.is_symlink() or not source.is_file() or source.is_symlink():
        raise ValidationError("skill candidate requires a regular candidate.json")
    try:
        manifest = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("candidate.json must be valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ValidationError("candidate manifest must be an object")
    candidate_id = manifest.get("candidate_id")
    if not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(candidate_id):
        raise ValidationError("candidate_id must be lowercase kebab-case")
    required = manifest.get("required_evaluation_paths")
    if not isinstance(required, list) or not required:
        raise ValidationError("candidate requires existing evaluation paths")
    if any(not isinstance(item, str) or not item.strip() for item in required):
        raise ValidationError("required evaluation paths must be non-empty strings")
    return root, manifest


def candidate_tree_sha256(candidate_dir: str | Path) -> str:
    root, _ = _manifest(candidate_dir)
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValidationError("skill candidate must not contain symlinks")
        if path.is_file():
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": sha256_file(path),
                }
            )
    if not entries:
        raise ValidationError("skill candidate must contain files")
    return sha256(canonical_json(entries).encode("utf-8")).hexdigest()


def skill_approval_text(
    candidate_id: str,
    candidate_hash: str,
    evaluation_event_id: str,
) -> str:
    return (
        f"Skill candidate {candidate_id} (SHA-256: {candidate_hash}) evaluated by "
        f"Event {evaluation_event_id} is APPROVED for promotion."
    )


def _event(company: CompanyOS, event_id: str) -> tuple[sqlite3.Row, dict[str, Any]]:
    row = company.store.query_one("SELECT * FROM events WHERE id = ?", (event_id,))
    if row is None:
        raise ValidationError(f"approval Event not found: {event_id}")
    return row, json.loads(row["payload_json"])


def evaluate_skill_candidate(
    company: CompanyOS,
    candidate_dir: str | Path,
    *,
    test_command: Sequence[str],
    idempotency_key: str,
    runner: Runner = subprocess.run,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Run every declared evaluation outside a write transaction and record it."""

    root, manifest = _manifest(candidate_dir)
    command = tuple(str(part) for part in test_command)
    if not command:
        raise ValidationError("skill evaluation command must not be empty")
    required_paths = [str(item) for item in manifest["required_evaluation_paths"]]
    if any(path not in command for path in required_paths):
        raise ValidationError("evaluation command must include every required path")
    before_hash = candidate_tree_sha256(root)
    command_payload = {
        "candidate_id": manifest["candidate_id"],
        "candidate_tree_sha256": before_hash,
        "test_command": list(command),
        "required_evaluation_paths": required_paths,
    }
    try:
        with company.store.transaction() as connection:
            claim = company.store.claim_idempotency(
                idempotency_key,
                "evaluate_skill_candidate",
                command_payload,
                connection=connection,
            )
            if not claim.is_new:
                if claim.completed and isinstance(claim.result, dict):
                    return dict(claim.result)
                raise ConflictError("skill evaluation is already in progress")
    except IdempotencyConflict as exc:
        raise ConflictError(str(exc)) from exc

    evaluation_id = new_id("skill_evaluation")
    try:
        completed = runner(
            command,
            cwd=company.root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        returncode = int(completed.returncode)
        stdout = str(completed.stdout or "")
        stderr = str(completed.stderr or "")
    except BaseException as error:
        returncode = -1
        stdout = ""
        stderr = f"{type(error).__name__}: {error}"
    after_hash = candidate_tree_sha256(root)
    unchanged = before_hash == after_hash
    passed = returncode == 0 and unchanged
    report = {
        "schema_version": 1,
        "evaluation_id": evaluation_id,
        "candidate_id": manifest["candidate_id"],
        "candidate_tree_sha256": after_hash,
        "required_evaluation_paths": required_paths,
        "test_command": list(command),
        "returncode": returncode,
        "candidate_unchanged": unchanged,
        "all_existing_evaluations_passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "stdout_tail": stdout[-20_000:],
        "stderr_tail": stderr[-20_000:],
        "created_at": utc_now(),
    }
    report_path = (
        company.root
        / "var"
        / "handoffs"
        / "skill-evaluations"
        / f"{evaluation_id}.json"
    )
    atomic_write_json(report_path, report)
    report_sha = sha256_file(report_path)
    relative_report = report_path.resolve().relative_to(company.root.resolve()).as_posix()

    with company.store.transaction() as connection:
        event = company.store.append_event(
            "SKILL_CANDIDATE_EVALUATED",
            aggregate_type="SkillCandidate",
            aggregate_id=str(manifest["candidate_id"]),
            payload={
                "evaluation_id": evaluation_id,
                "candidate_tree_sha256": after_hash,
                "evaluation_report_path": relative_report,
                "evaluation_report_sha256": report_sha,
                "verdict": report["verdict"],
                "all_existing_evaluations_passed": passed,
            },
            connection=connection,
        )
        result = {
            "status": "PASS" if passed else "FAIL",
            "candidate_id": str(manifest["candidate_id"]),
            "event_id": str(event["id"]),
            "report_path": str(report_path),
            "report_sha256": report_sha,
            "candidate_tree_sha256": after_hash,
        }
        company.store.complete_idempotency(
            idempotency_key,
            result,
            command="evaluate_skill_candidate",
            connection=connection,
        )
    return result


def approve_skill_candidate(
    company: CompanyOS,
    candidate_dir: str | Path,
    *,
    evaluation_event_id: str,
    approval_text: str,
    idempotency_key: str,
) -> dict[str, Any]:
    root, manifest = _manifest(candidate_dir)
    candidate_hash = candidate_tree_sha256(root)
    evaluation_event, evaluation = _event(company, evaluation_event_id)
    if (
        evaluation_event["event_type"] != "SKILL_CANDIDATE_EVALUATED"
        or evaluation_event["aggregate_id"] != manifest["candidate_id"]
        or evaluation.get("candidate_tree_sha256") != candidate_hash
        or evaluation.get("verdict") != "PASS"
        or evaluation.get("all_existing_evaluations_passed") is not True
    ):
        raise ValidationError("candidate does not have a current passing evaluation")
    expected_approval = skill_approval_text(
        str(manifest["candidate_id"]),
        candidate_hash,
        evaluation_event_id,
    )
    if approval_text.strip() != expected_approval:
        raise ValidationError("CEO skill approval text does not match the candidate hash")
    command_payload = {
        "candidate_id": manifest["candidate_id"],
        "candidate_tree_sha256": candidate_hash,
        "evaluation_event_id": evaluation_event_id,
        "evaluation_report_sha256": evaluation["evaluation_report_sha256"],
        "approval_text_sha256": sha256(
            approval_text.strip().encode("utf-8")
        ).hexdigest(),
    }

    def operation(connection: sqlite3.Connection) -> dict[str, Any]:
        event = company.store.append_event(
            "CEO_SKILL_PROMOTION_APPROVED",
            aggregate_type="SkillCandidate",
            aggregate_id=str(manifest["candidate_id"]),
            payload={
                **command_payload,
                "actor": "CEO",
                "source": "LOCAL_CLI",
            },
            connection=connection,
        )
        return {
            "status": "APPROVED",
            "candidate_id": str(manifest["candidate_id"]),
            "event_id": str(event["id"]),
            "candidate_tree_sha256": candidate_hash,
        }

    return dict(
        company.store.run_idempotent(
            idempotency_key,
            "approve_skill_candidate",
            command_payload,
            operation,
        )
    )


def promote_skill_candidate(
    company: CompanyOS,
    candidate_dir: str | Path,
    *,
    approval_event_id: str,
    destination_root: str | Path | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    root, manifest = _manifest(candidate_dir)
    candidate_hash = candidate_tree_sha256(root)
    approval_event, approval = _event(company, approval_event_id)
    if (
        approval_event["event_type"] != "CEO_SKILL_PROMOTION_APPROVED"
        or approval_event["aggregate_id"] != manifest["candidate_id"]
        or approval.get("candidate_tree_sha256") != candidate_hash
        or approval.get("actor") != "CEO"
    ):
        raise ValidationError("approval Event does not bind the current candidate")
    target_root = Path(
        destination_root or company.root / "lines" / "chatbot" / "skills"
    ).resolve()
    destination = target_root / str(manifest["candidate_id"])
    command_payload = {
        "candidate_id": manifest["candidate_id"],
        "candidate_tree_sha256": candidate_hash,
        "approval_event_id": approval_event_id,
        "destination": str(destination),
    }

    def operation(connection: sqlite3.Connection) -> dict[str, Any]:
        if destination.exists():
            raise ValidationError(f"promoted skill already exists: {destination}")
        staging = target_root / f".{manifest['candidate_id']}.{new_id('staging')}"
        target_root.mkdir(parents=True, exist_ok=True)
        moved = False
        try:
            shutil.copytree(root, staging)
            if candidate_tree_sha256(staging) != candidate_hash:
                raise ValidationError("staged candidate hash changed during promotion")
            staging.replace(destination)
            moved = True
            event = company.store.append_event(
                "SKILL_CANDIDATE_PROMOTED",
                aggregate_type="SkillCandidate",
                aggregate_id=str(manifest["candidate_id"]),
                payload={
                    **command_payload,
                    "evaluation_event_id": approval["evaluation_event_id"],
                    "evaluation_report_sha256": approval[
                        "evaluation_report_sha256"
                    ],
                },
                connection=connection,
            )
            return {
                "status": "PROMOTED",
                "candidate_id": str(manifest["candidate_id"]),
                "event_id": str(event["id"]),
                "destination": str(destination),
                "candidate_tree_sha256": candidate_hash,
            }
        except BaseException:
            if moved and destination.exists():
                shutil.rmtree(destination)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    result = company.store.run_idempotent(
        idempotency_key,
        "promote_skill_candidate",
        command_payload,
        operation,
    )
    assert isinstance(result, dict)
    return dict(result)
