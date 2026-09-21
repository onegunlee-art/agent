from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class Idea:
    id: str
    text: str
    created_at: str


@dataclass(frozen=True)
class CouncilRequest:
    role: str
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class CompileOutcome:
    contract_id: str
    gate_result: Any


@dataclass(frozen=True)
class Venture:
    id: str
    contract_id: str
    idea_id: str
    workspace_path: Path
    context_manifest_path: Path
    status: str


@dataclass(frozen=True)
class WorkOrder:
    id: str
    venture_id: str
    title: str
    status: str
    artifact_relative_path: str
    expected_content: str
    verifier_path: Path
    verifier_hash: str


@dataclass(frozen=True)
class Run:
    id: str
    work_order_id: str
    status: str
    attempt: int
    verifier_hash: str


@dataclass(frozen=True)
class Evidence:
    id: str
    venture_id: str
    work_order_id: str
    run_id: str
    kind: str
    path: Path
    sha256: str | None
    trusted: bool


@dataclass(frozen=True)
class Review:
    id: str
    work_order_id: str
    status: str
    json_path: Path
    markdown_path: Path
    request_hash: str


@dataclass(frozen=True)
class ExecutionOutput:
    artifact_path: Path


class ExecutorPort(Protocol):
    name: str

    def execute(self, work_order: WorkOrder, workspace_path: Path) -> ExecutionOutput:
        """Create or supply the artifact for a WorkOrder."""


class ReviewerPort(Protocol):
    name: str

    def review(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return a schema-valid review result for automated tests only."""
