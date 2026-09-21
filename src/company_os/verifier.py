from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .utils import atomic_write_json, read_json, sha256_file


@dataclass(frozen=True)
class VerifierResult:
    status: str
    message: str
    artifact_path: Path
    artifact_sha256: str | None
    details: dict[str, Any]


def write_exact_text_verifier(
    path: Path,
    *,
    artifact_relative_path: str,
    expected_content: str,
) -> str:
    spec = {
        "schema_version": 1,
        "kind": "EXACT_TEXT",
        "artifact_relative_path": artifact_relative_path,
        "expected_content": expected_content,
        "encoding": "utf-8",
    }
    atomic_write_json(path, spec)
    return sha256_file(path)


def verifier_hash(path: Path) -> str:
    return sha256_file(path)


def run_verifier(spec_path: Path, workspace_path: Path) -> VerifierResult:
    spec = read_json(spec_path)
    if spec.get("kind") != "EXACT_TEXT":
        raise ValueError("Unsupported verifier kind")

    root = workspace_path.resolve()
    artifact_path = (root / str(spec["artifact_relative_path"])).resolve()
    if root not in artifact_path.parents:
        raise ValueError("Verifier artifact path escapes Venture workspace")
    if not artifact_path.is_file():
        return VerifierResult(
            status="FAIL",
            message="Expected artifact does not exist.",
            artifact_path=artifact_path,
            artifact_sha256=None,
            details={"reason": "MISSING_ARTIFACT"},
        )

    actual = artifact_path.read_text(encoding=str(spec.get("encoding", "utf-8")))
    expected = str(spec["expected_content"])
    status = "PASS" if actual == expected else "FAIL"
    return VerifierResult(
        status=status,
        message="Artifact content matched." if status == "PASS" else "Artifact content differed.",
        artifact_path=artifact_path,
        artifact_sha256=sha256_file(artifact_path),
        details={
            "reason": "MATCH" if status == "PASS" else "CONTENT_MISMATCH",
            "actual_length": len(actual),
            "expected_length": len(expected),
        },
    )


def result_as_dict(result: VerifierResult) -> dict[str, Any]:
    data = asdict(result)
    data["artifact_path"] = str(result.artifact_path)
    return data
