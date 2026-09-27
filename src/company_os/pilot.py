"""Hash-bound, synthetic-safe helpers for the first customer pilot."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Sequence

from .errors import ValidationError
from .utils import atomic_write_json, canonical_json, sha256_file, utc_now


_CUSTOMER_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _customer_id(value: str) -> str:
    if _CUSTOMER_ID.fullmatch(value) is None:
        raise ValidationError("customer_id must be a bounded lowercase identifier")
    return value


def evaluation_approval_text_v2(
    *,
    customer_id: str,
    spec_path: str,
    case_count: int,
    spec_sha256: str,
    threshold: float,
) -> str:
    """Return the exact CEO sentence for customer-aware evaluation approval."""

    customer = _customer_id(customer_id)
    normalized_path = spec_path.replace("\\", "/").strip("/")
    if (
        not normalized_path
        or normalized_path.startswith("../")
        or "/../" in f"/{normalized_path}/"
    ):
        raise ValidationError("evaluation spec path must be repository-relative")
    if case_count <= 0:
        raise ValidationError("evaluation case_count must be positive")
    if _SHA256.fullmatch(spec_sha256) is None:
        raise ValidationError("evaluation spec SHA-256 must be lowercase hex")
    if threshold <= 0 or threshold > 1:
        raise ValidationError("evaluation threshold must be in (0, 1]")
    return (
        f"고객 {customer}의 평가 사례 {case_count}건(경로: {normalized_path}, "
        f"SHA-256: {spec_sha256})과 threshold {threshold:.2f}을 APPROVED로 "
        "승인합니다."
    )


def create_delivery_report(
    output_path: str | Path,
    *,
    customer_id: str,
    source_commit: str,
    source_tree_oid: str,
    model_run_id: str,
    model_execution_id: str,
    evaluation_evidence_id: str,
    evaluation_report_sha256: str,
    evaluation_approval_event_id: str,
    skill_promotion_event_id: str,
    review_verdict_sha256: str,
    review_provenance: str,
    artifacts: Sequence[tuple[str, Path]],
    lifecycle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write an unsigned DSSE-compatible delivery manifest and its bindings."""

    customer = _customer_id(customer_id)
    for label, value in (
        ("source commit", source_commit),
        ("source tree OID", source_tree_oid),
    ):
        if re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValidationError(f"{label} must be a Git SHA-1")
    for label, value in (
        ("evaluation report", evaluation_report_sha256),
        ("review verdict", review_verdict_sha256),
    ):
        if _SHA256.fullmatch(value) is None:
            raise ValidationError(f"{label} SHA-256 must be lowercase hex")
    if review_provenance not in {
        "ORIGINAL",
        "USER_SUPPLIED_TRANSCRIPT",
        "USER_SUPPLIED_TRANSCRIPT_SUMMARY",
    }:
        raise ValidationError("unsupported review verdict provenance")

    files: list[dict[str, Any]] = []
    labels: set[str] = set()
    for label, source in artifacts:
        normalized = label.replace("\\", "/").strip("/")
        if not normalized or normalized.startswith("../") or normalized in labels:
            raise ValidationError("delivery artifact labels must be unique relative paths")
        path = Path(source).resolve()
        if path.is_symlink() or not path.is_file():
            raise ValidationError("delivery artifact must be a regular file")
        labels.add(normalized)
        files.append(
            {
                "path": normalized,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    files.sort(key=lambda item: str(item["path"]))
    manifest_core = {"files": files}
    manifest_sha256 = hashlib.sha256(
        canonical_json(manifest_core).encode("utf-8")
    ).hexdigest()
    payload = {
        "schema_version": 1,
        "customer_id": customer,
        "status": "DELIVERY_CANDIDATE",
        "source": {"commit": source_commit, "tree_oid": source_tree_oid},
        "model": {
            "run_id": model_run_id,
            "execution_id": model_execution_id,
        },
        "evaluation": {
            "evidence_id": evaluation_evidence_id,
            "report_sha256": evaluation_report_sha256,
        },
        "approvals": {
            "evaluation_event_id": evaluation_approval_event_id,
            "skill_promotion_event_id": skill_promotion_event_id,
        },
        "review": {
            "sha256": review_verdict_sha256,
            "provenance": review_provenance,
        },
        "manifest": {**manifest_core, "sha256": manifest_sha256},
        "lifecycle": dict(lifecycle or {}),
        "created_at": utc_now(),
    }
    delivery_payload_sha256 = hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()
    payload["signature_envelope"] = {
        "format": "DSSE_COMPATIBLE_UNSIGNED",
        "payload_type": "application/vnd.ai-company-os.delivery-report+json",
        "payload_sha256": delivery_payload_sha256,
        "signatures": [],
    }
    atomic_write_json(Path(output_path), payload)
    return payload

