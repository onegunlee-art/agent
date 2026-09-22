from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .handoffs import markdown_document
from .models import ExecutionOutput, Idea, Review, WorkOrder
from .roles import synthetic_council_response
from .utils import atomic_write_json, atomic_write_text, contained_path, read_json


@dataclass(frozen=True)
class _FakeExecutive:
    role: str

    def response(self, idea: Idea) -> dict[str, Any]:
        return synthetic_council_response(self.role, idea.id, idea.text)

    def write_response(self, root: Path, idea: Idea) -> Path:
        directory = contained_path(
            root, "var", "handoffs", "council", idea.id
        )
        path = directory / f"{self.role}_response.json"
        payload = self.response(idea)
        atomic_write_json(path, payload)
        atomic_write_text(
            directory / f"{self.role}_response.md",
            markdown_document(f"Synthetic {self.role.upper()} Response", payload),
        )
        return path


class FakeCTO(_FakeExecutive):
    def __init__(self) -> None:
        super().__init__(role="cto")


class FakeCPO(_FakeExecutive):
    def __init__(self) -> None:
        super().__init__(role="cpo")


class FakeCMO(_FakeExecutive):
    def __init__(self) -> None:
        super().__init__(role="cmo")


class FakeExecutor:
    """Deterministic executor used only by automated tests."""

    name = "fake_executor"

    def execute(self, work_order: WorkOrder, workspace_path: Path) -> ExecutionOutput:
        artifact_path = contained_path(
            workspace_path, work_order.artifact_relative_path
        )
        atomic_write_text(artifact_path, work_order.expected_content)
        return ExecutionOutput(artifact_path=artifact_path)


class FakeReviewer:
    """Deterministic reviewer used only by automated tests."""

    name = "fake_reviewer"

    def result(self, review: Review) -> dict[str, Any]:
        request = read_json(review.json_path)
        return {
            "schema_version": 2,
            "review_request_id": review.id,
            "review_request_hash": review.request_hash,
            "reviewed_commit": request["source_commit"],
            "reviewed_tree_sha256": request["source_tree_sha256"],
            "source": "fake_reviewer",
            "requested_reviewer": "Claude Opus 5",
            "actual_reviewer": "FakeReviewer",
            "verdict": "CHANGES_REQUIRED",
            "findings": [
                {
                    "code": "SYNTHETIC_REPAIR_REQUIRED",
                    "severity": "LOW",
                    "message": "Exercise exactly one deterministic repair cycle.",
                }
            ],
            "required_changes": [
                {
                    "id": "synthetic-change-1",
                    "description": "Regenerate and reverify the synthetic artifact once.",
                }
            ],
        }

    def write_result(self, root: Path, review: Review) -> Path:
        directory = review.json_path.parent
        path = directory / "fake_review_result.json"
        atomic_write_json(path, self.result(review))
        return path
