"""Deterministic source-diff materials for independent review packages."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError
from .utils import atomic_write_bytes, atomic_write_json, atomic_write_text, sha256_file


@dataclass(frozen=True)
class ReviewMaterials:
    patch_path: Path
    binding_path: Path
    request_path: Path
    baseline_commit: str
    baseline_tree_oid: str
    target_commit: str
    target_tree_oid: str
    patch_sha256: str


def _git_bytes(repository: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValidationError(f"git review-package command failed: {' '.join(args)}") from exc
    return completed.stdout


def _git_text(repository: Path, *args: str) -> str:
    return _git_bytes(repository, *args).decode("ascii").strip()


def build_review_materials(
    repository: str | Path,
    *,
    baseline_commit: str,
    target_commit: str,
    output_dir: str | Path,
) -> ReviewMaterials:
    """Write an LF-preserving binary patch plus both commit/tree bindings."""

    repo = Path(repository).resolve()
    if not (repo / ".git").exists():
        raise ValidationError("review package source must be a Git repository")
    baseline = _git_text(repo, "rev-parse", "--verify", f"{baseline_commit}^{{commit}}")
    target = _git_text(repo, "rev-parse", "--verify", f"{target_commit}^{{commit}}")
    baseline_tree = _git_text(repo, "rev-parse", f"{baseline}^{{tree}}")
    target_tree = _git_text(repo, "rev-parse", f"{target}^{{tree}}")
    patch = _git_bytes(repo, "diff", "--binary", baseline, target, "--")
    if not patch:
        raise ValidationError("review package patch must not be empty")
    if b"\r\n" in patch:
        raise ValidationError("git emitted CRLF patch bytes; refusing non-portable package")

    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    patch_path = destination / "review.patch"
    binding_path = destination / "source-binding.json"
    request_path = destination / "review-request.md"
    atomic_write_bytes(patch_path, patch)
    patch_digest = sha256_file(patch_path)
    atomic_write_json(
        binding_path,
        {
            "schema_version": 1,
            "baseline_commit": baseline,
            "baseline_tree_oid": baseline_tree,
            "target_commit": target,
            "target_tree_oid": target_tree,
            "patch_sha256": patch_digest,
            "patch_line_endings": "LF",
            "patch_mode": "git diff --binary <baseline> <target>",
        },
    )
    atomic_write_text(
        request_path,
        "\n".join(
            (
                "# Independent review request — source binding",
                "",
                f"- baseline commit: `{baseline}`",
                f"- baseline tree OID: `{baseline_tree}`",
                f"- target commit: `{target}`",
                f"- target tree OID: `{target_tree}`",
                f"- patch SHA-256: `{patch_digest}`",
                "- patch encoding: raw `git diff --binary` bytes with LF line endings",
                "",
                "The review scope and acceptance criteria must be appended without "
                "changing the source-binding lines above.",
                "",
            )
        ),
    )
    return ReviewMaterials(
        patch_path,
        binding_path,
        request_path,
        baseline,
        baseline_tree,
        target,
        target_tree,
        patch_digest,
    )
