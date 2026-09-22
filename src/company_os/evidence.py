"""Validation for explicit, change-specific Evidence receipts."""

from __future__ import annotations

import json
from typing import Any, Iterable

from .errors import ValidationError


def validate_test_result_receipt(
    content: bytes,
    *,
    selected_node_ids: Iterable[str],
    source_commit: str,
    source_tree_sha256: str | None,
) -> dict[str, Any]:
    """Validate a deterministic PASS receipt and return its parsed payload."""

    try:
        receipt = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("TEST_RESULT must be a UTF-8 JSON receipt") from exc
    if not isinstance(receipt, dict):
        raise ValidationError("TEST_RESULT receipt must be a JSON object")
    if receipt.get("schema_version") != 1:
        raise ValidationError("Unsupported TEST_RESULT receipt schema_version")
    if receipt.get("kind") != "PYTEST_RESULT":
        raise ValidationError("TEST_RESULT receipt kind must be PYTEST_RESULT")
    if receipt.get("status") != "PASSED":
        raise ValidationError("TEST_RESULT receipt status must be PASSED")
    exit_code = receipt.get("exit_code")
    if type(exit_code) is not int or exit_code != 0:
        raise ValidationError("TEST_RESULT receipt exit_code must be 0")
    if receipt.get("source_commit") != source_commit:
        raise ValidationError("TEST_RESULT receipt source_commit does not match")
    receipt_tree = receipt.get("source_tree_sha256")
    if (
        not isinstance(receipt_tree, str)
        or len(receipt_tree) != 64
        or any(character not in "0123456789abcdef" for character in receipt_tree)
    ):
        raise ValidationError(
            "TEST_RESULT receipt source_tree_sha256 must be lowercase SHA-256"
        )
    if source_tree_sha256 is not None and receipt_tree != source_tree_sha256:
        raise ValidationError("TEST_RESULT receipt source_tree_sha256 does not match")

    tests = receipt.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ValidationError("TEST_RESULT receipt requires passed test records")
    outcomes: dict[str, str] = {}
    for item in tests:
        if not isinstance(item, dict):
            raise ValidationError("TEST_RESULT test records must be objects")
        node_id = item.get("node_id")
        outcome = item.get("outcome")
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValidationError("TEST_RESULT test node_id must not be empty")
        normalized_node = node_id.strip()
        if normalized_node in outcomes:
            raise ValidationError("TEST_RESULT receipt test node_ids must be unique")
        if outcome != "PASSED":
            raise ValidationError("Every TEST_RESULT test outcome must be PASSED")
        outcomes[normalized_node] = outcome

    selected = {node.strip() for node in selected_node_ids}
    missing = sorted(selected.difference(outcomes))
    if missing:
        raise ValidationError(
            "TEST_RESULT selected node_ids are absent from the PASS receipt: "
            + ", ".join(missing)
        )
    return receipt


__all__ = ["validate_test_result_receipt"]
