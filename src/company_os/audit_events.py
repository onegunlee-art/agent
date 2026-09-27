"""Dedicated, append-only audit commands for reviewed verdicts and source pushes."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Sequence

from .application import CompanyOS
from .errors import ConflictError, ValidationError
from .storage import IdempotencyConflict
from .utils import sha256_file


SOURCE_PUSH_APPROVAL_TEXT = (
    "이번 1회에 한해 origin main과 모든 로컬 태그 push를 승인합니다."
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA1 = re.compile(r"[0-9a-f]{40}")
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")


def _relative_or_absolute(root: Path, source: Path) -> str:
    try:
        return source.relative_to(root).as_posix()
    except ValueError:
        return str(source)


def _append_once(
    company: CompanyOS,
    *,
    command: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    try:
        with company.store.transaction() as connection:
            claim = company.store.claim_idempotency(
                idempotency_key,
                command,
                payload,
                connection=connection,
            )
            if not claim.is_new:
                if claim.completed and isinstance(claim.result, dict):
                    return dict(claim.result)
                raise ConflictError(f"{command} is already in progress")
            event = company.store.append_event(
                event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload=payload,
                connection=connection,
            )
            result = {
                "status": "RECORDED",
                "event_type": event_type,
                "event_id": str(event["id"]),
                **payload,
            }
            company.store.complete_idempotency(
                idempotency_key,
                result,
                command=command,
                connection=connection,
            )
            return result
    except IdempotencyConflict as exc:
        raise ConflictError(str(exc)) from exc


def record_claude_verdict_archived(
    company: CompanyOS,
    verdict_file: str | Path,
    *,
    expected_sha256: str,
    provenance: str,
    idempotency_key: str,
) -> dict[str, Any]:
    source = Path(verdict_file).resolve()
    if source.is_symlink() or not source.is_file():
        raise ValidationError("Claude verdict must be a regular file")
    if _SHA256.fullmatch(expected_sha256) is None:
        raise ValidationError("Claude verdict SHA-256 must be lowercase hex")
    actual = sha256_file(source)
    if actual != expected_sha256:
        raise ValidationError("Claude verdict SHA-256 does not match")
    if provenance not in {
        "ORIGINAL",
        "USER_SUPPLIED_TRANSCRIPT",
        "USER_SUPPLIED_TRANSCRIPT_SUMMARY",
    }:
        raise ValidationError("unsupported Claude verdict provenance")
    payload = {
        "path": _relative_or_absolute(company.root, source),
        "sha256": actual,
        "provenance": provenance,
        "content_claim": "ORIGINAL" if provenance == "ORIGINAL" else "TRANSCRIPT_ONLY",
        "actor": "SYSTEM",
    }
    return _append_once(
        company,
        command="archive_claude_verdict",
        event_type="CLAUDE_VERDICT_ARCHIVED",
        aggregate_type="ReviewVerdict",
        aggregate_id=actual,
        payload=payload,
        idempotency_key=idempotency_key,
    )


def record_source_pushed(
    company: CompanyOS,
    *,
    remote_url: str,
    commit: str,
    tags: Sequence[str],
    approval_file: str | Path,
    idempotency_key: str,
) -> dict[str, Any]:
    if not remote_url.startswith(("https://", "ssh://", "git@")):
        raise ValidationError("source push remote URL is invalid")
    if _GIT_SHA1.fullmatch(commit) is None:
        raise ValidationError("source push commit must be a Git SHA-1")
    normalized_tags = sorted(set(str(tag).strip() for tag in tags))
    if not normalized_tags or any(_TAG.fullmatch(tag) is None for tag in normalized_tags):
        raise ValidationError("source push requires valid local tags")
    approval = Path(approval_file).resolve()
    if approval.is_symlink() or not approval.is_file():
        raise ValidationError("source push approval must be a regular file")
    text = approval.read_text(encoding="utf-8").strip()
    if text != SOURCE_PUSH_APPROVAL_TEXT:
        raise ValidationError("source push approval text does not exactly match CEO approval")
    payload = {
        "remote_url": remote_url,
        "commit": commit,
        "tags": normalized_tags,
        "push_scope": ["origin/main", "all-local-tags"],
        "approval_text": text,
        "approval_sha256": sha256_file(approval),
        "actor": "CEO",
    }
    return _append_once(
        company,
        command="record_source_pushed",
        event_type="SOURCE_PUSHED",
        aggregate_type="Source",
        aggregate_id=commit,
        payload=payload,
        idempotency_key=idempotency_key,
    )
