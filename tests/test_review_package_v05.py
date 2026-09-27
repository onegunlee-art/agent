from __future__ import annotations

import json
from pathlib import Path
import subprocess

from company_os.cli import build_parser
from company_os.review_package import build_review_materials
from company_os.utils import sha256_file


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def test_review_patch_is_lf_binary_safe_and_binds_baseline_tree(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Synthetic Review")
    _git(repository, "config", "user.email", "review@example.invalid")
    _git(repository, "config", "core.autocrlf", "false")
    (repository / "tracked.txt").write_bytes(b"baseline\n")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-q", "-m", "baseline")
    baseline = _git(repository, "rev-parse", "HEAD")
    baseline_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    (repository / "tracked.txt").write_bytes(b"target\n")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-q", "-m", "target")
    target = _git(repository, "rev-parse", "HEAD")

    result = build_review_materials(
        repository,
        baseline_commit=baseline,
        target_commit=target,
        output_dir=tmp_path / "handoff",
    )
    patch_bytes = result.patch_path.read_bytes()
    binding = json.loads(result.binding_path.read_text(encoding="utf-8"))

    assert b"\r\n" not in patch_bytes
    assert patch_bytes.startswith(b"diff --git ")
    assert binding["baseline_commit"] == baseline
    assert binding["baseline_tree_oid"] == baseline_tree
    assert binding["target_commit"] == target
    assert binding["patch_sha256"] == sha256_file(result.patch_path)
    request_text = result.request_path.read_text(encoding="utf-8")
    assert f"baseline commit: `{baseline}`" in request_text
    assert f"baseline tree OID: `{baseline_tree}`" in request_text
    assert f"target commit: `{target}`" in request_text
    assert f"patch SHA-256: `{binding['patch_sha256']}`" in request_text

    reconstructed = tmp_path / "reconstructed"
    subprocess.run(
        ["git", "clone", "-q", "--no-hardlinks", str(repository), str(reconstructed)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _git(reconstructed, "checkout", "-q", target)
    _git(reconstructed, "apply", "--binary", "-R", str(result.patch_path))
    _git(reconstructed, "add", "-A")
    assert _git(reconstructed, "write-tree") == baseline_tree


def test_review_package_cli_requires_baseline_and_target_tree_binding() -> None:
    parsed = build_parser().parse_args(
        [
            "review",
            "package",
            "--baseline",
            "a" * 40,
            "--target",
            "b" * 40,
            "--output-dir",
            "handoff",
        ]
    )

    assert parsed.review_command == "package"
    assert parsed.baseline == "a" * 40
    assert parsed.target == "b" * 40
