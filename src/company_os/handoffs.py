from __future__ import annotations

import json
from typing import Any, Iterable

from .errors import ValidationError


MODEL_METADATA = {
    "requested_model": "GPT-5.6 Sol",
    "resolved_model": "current_cursor_codex_session",
    "execution_surface": "Cursor Codex",
    "authentication_mode": "ChatGPT subscription",
}

REVIEWER_METADATA = {
    "requested_reviewer": "Claude Opus 5",
    "integration": "structured_manual_file_handoff",
    "authentication_mode": "Claude subscription",
}


def markdown_document(title: str, payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"# {title}\n\n```json\n{serialized}\n```\n"


def require_fields(
    mapping: dict[str, Any],
    fields: Iterable[str],
    *,
    context: str,
) -> None:
    missing = [field for field in fields if field not in mapping]
    if missing:
        raise ValidationError(f"{context} missing fields: {', '.join(missing)}")


def validate_council_response(
    payload: dict[str, Any],
    *,
    idea_id: str,
    role: str,
    required_outputs: Iterable[str],
) -> None:
    require_fields(
        payload,
        (
            "schema_version",
            "idea_id",
            "role",
            "model_metadata",
            "outputs",
            "contract_contribution",
        ),
        context="Council response",
    )
    if payload["schema_version"] != 1:
        raise ValidationError("Unsupported council response schema_version")
    if payload["idea_id"] != idea_id:
        raise ValidationError("Council response idea_id does not match")
    if payload["role"] != role:
        raise ValidationError("Council response role does not match")
    if not isinstance(payload["outputs"], dict):
        raise ValidationError("Council response outputs must be an object")
    require_fields(payload["outputs"], required_outputs, context=f"{role.upper()} outputs")
    for field in required_outputs:
        value = payload["outputs"][field]
        if value is None or (isinstance(value, (str, list, dict)) and not value):
            raise ValidationError(
                f"{role.upper()} outputs field must not be empty: {field}"
            )
    if not isinstance(payload["contract_contribution"], dict):
        raise ValidationError("contract_contribution must be an object")


def validate_review_result(
    payload: dict[str, Any],
    *,
    request_id: str,
    request_hash: str,
) -> None:
    require_fields(
        payload,
        (
            "schema_version",
            "review_request_id",
            "review_request_hash",
            "source",
            "verdict",
            "findings",
            "required_changes",
        ),
        context="ReviewResult",
    )
    if payload["schema_version"] != 1:
        raise ValidationError("Unsupported ReviewResult schema_version")
    if payload["review_request_id"] != request_id:
        raise ValidationError("ReviewResult request id does not match")
    if payload["review_request_hash"] != request_hash:
        raise ValidationError("ReviewResult request hash does not match")
    if payload["verdict"] not in {"PASS", "CHANGES_REQUIRED"}:
        raise ValidationError("ReviewResult verdict is invalid")
    if payload["source"] not in {"user_supplied", "fake_reviewer"}:
        raise ValidationError(
            "ReviewResult source must be user_supplied or fake_reviewer"
        )
    if not isinstance(payload["findings"], list):
        raise ValidationError("ReviewResult findings must be a list")
    if not isinstance(payload["required_changes"], list):
        raise ValidationError("ReviewResult required_changes must be a list")
    for index, finding in enumerate(payload["findings"]):
        if not isinstance(finding, dict) or not all(
            isinstance(finding.get(field), str) and finding[field].strip()
            for field in ("code", "message")
        ):
            raise ValidationError(
                f"ReviewResult findings[{index}] requires code and message"
            )
    for index, change in enumerate(payload["required_changes"]):
        if not isinstance(change, dict) or not all(
            isinstance(change.get(field), str) and change[field].strip()
            for field in ("id", "description")
        ):
            raise ValidationError(
                f"ReviewResult required_changes[{index}] requires id and description"
            )
    if payload["verdict"] == "CHANGES_REQUIRED" and not payload["required_changes"]:
        raise ValidationError("CHANGES_REQUIRED must include required_changes")
