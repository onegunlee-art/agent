from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import importlib
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys

import pytest

from company_os.source_snapshot import (
    GitSourceSnapshot,
    SourceSnapshot,
    SourceSnapshotError,
    SourceSnapshotPort,
)


SUBPROCESS_TIMEOUT_SECONDS = 20


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git is required for source snapshot tests")

    repo = tmp_path / "source"
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Snapshot Test")
    _git(repo, "config", "user.email", "snapshot@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "core.filemode", "false")
    (repo / "alpha.txt").write_bytes(b"alpha\n")
    nested = repo / "nested"
    nested.mkdir()
    (nested / "beta.bin").write_bytes(b"\x00beta\xff")
    _git(repo, "add", "--", "alpha.txt", "nested/beta.bin")
    _git(repo, "commit", "--quiet", "-m", "initial")
    return repo


def _frame(value: bytes) -> bytes:
    return len(value).to_bytes(8, "big") + value


def _expected_initial_manifest_sha256() -> str:
    entries = [
        (b"alpha.txt", b"100644", sha256(b"alpha\n").digest()),
        (b"nested/beta.bin", b"100644", sha256(b"\x00beta\xff").digest()),
    ]
    manifest = bytearray(b"ai-company-os-source-tree-v1\0")
    manifest.extend(len(entries).to_bytes(8, "big"))
    for path, mode, content_digest in entries:
        manifest.extend(_frame(path))
        manifest.extend(_frame(mode))
        manifest.extend(content_digest)
    return sha256(manifest).hexdigest()


def test_clean_snapshot_reports_commit_tree_and_deterministic_sha256(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    source = GitSourceSnapshot(code_root=repo)

    first = source.capture()
    second = source.capture()

    assert isinstance(first, SourceSnapshot)
    assert isinstance(source, SourceSnapshotPort)
    assert first == second
    assert first.source_commit == _git(repo, "rev-parse", "HEAD")
    assert first.source_tree_oid == _git(repo, "rev-parse", "HEAD^{tree}")
    assert first.source_tree_sha256 == _expected_initial_manifest_sha256()
    assert first.dirty is False


def test_dirty_or_staged_tracked_content_is_rejected_until_committed(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    source = GitSourceSnapshot(code_root=repo)
    clean = source.capture()

    (repo / "alpha.txt").write_bytes(b"changed tracked content\n")
    with pytest.raises(SourceSnapshotError, match="Tracked source must be clean"):
        source.capture()

    _git(repo, "add", "--", "alpha.txt")
    with pytest.raises(SourceSnapshotError, match="Tracked source must be clean"):
        source.capture()

    _git(repo, "commit", "--quiet", "-m", "change alpha")
    committed = source.capture()
    assert committed.dirty is False
    assert committed.source_commit != clean.source_commit
    assert committed.source_tree_oid != clean.source_tree_oid
    assert committed.source_tree_sha256 != clean.source_tree_sha256


def test_untracked_content_is_rejected_instead_of_being_excluded_from_hash(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    source = GitSourceSnapshot(code_root=repo)

    (repo / "untracked.py").write_text(
        "raise RuntimeError('not reviewed')\n",
        encoding="utf-8",
    )

    with pytest.raises(SourceSnapshotError, match="Untracked path prevents"):
        source.capture()


def test_allowlisted_ignored_runtime_roots_do_not_dirty_the_snapshot(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    source = GitSourceSnapshot(code_root=repo)
    baseline = source.capture()
    (repo / ".git" / "info" / "exclude").write_text(
        ".venv/\nvar/\n",
        encoding="utf-8",
    )
    runtime_module = repo / ".venv" / "Lib" / "site-packages" / "runtime.py"
    runtime_module.parent.mkdir(parents=True)
    runtime_module.write_text("RUNTIME_ONLY = True\n", encoding="utf-8")
    state_db = repo / "var" / "state" / "company.db"
    state_db.parent.mkdir(parents=True)
    state_db.write_bytes(b"local runtime state")
    junit = repo / "var" / "handoffs" / "product-tests.xml"
    junit.parent.mkdir(parents=True)
    junit.write_text('<testsuites tests="1" failures="0"/>\n', encoding="utf-8")

    with_runtime = source.capture()

    assert with_runtime == baseline
    assert with_runtime.dirty is False


@pytest.mark.parametrize("name", ["plugin.py", "plugin.pyz", "plugin"])
def test_unknown_file_type_inside_ignored_runtime_root_is_rejected(
    tmp_path: Path,
    name: str,
) -> None:
    repo = _repository(tmp_path)
    (repo / ".git" / "info" / "exclude").write_text(
        "var\n",
        encoding="utf-8",
    )
    plugin = repo / "var" / name
    plugin.parent.mkdir(parents=True)
    plugin.write_text("raise RuntimeError('not reviewed')\n", encoding="utf-8")

    with pytest.raises(SourceSnapshotError, match="file type is not permitted"):
        GitSourceSnapshot(code_root=repo).capture()


def test_ignored_runtime_root_symlink_is_rejected(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    (repo / ".git" / "info" / "exclude").write_text(
        "var\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside-runtime"
    outside.mkdir()
    (outside / "state.json").write_text("{}\n", encoding="utf-8")
    try:
        (repo / "var").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(SourceSnapshotError, match="Symlinks and junctions"):
        GitSourceSnapshot(code_root=repo).capture()


def test_project_bytecode_cache_is_rejected_even_when_importable(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    module_name = "snapshot_bytecode_victim"
    source_path = repo / f"{module_name}.py"
    safe_source = "VALUE = 'SAFE'\n"
    evil_source = "VALUE = 'EVIL'\n"
    assert len(safe_source) == len(evil_source)
    source_path.write_text(safe_source, encoding="utf-8")
    _git(repo, "add", "--", source_path.name)
    _git(repo, "commit", "--quiet", "-m", "add import victim")
    (repo / ".git" / "info" / "exclude").write_text(
        "__pycache__/\n",
        encoding="utf-8",
    )

    timestamp = int(source_path.stat().st_mtime)
    source_path.write_text(evil_source, encoding="utf-8")
    os.utime(source_path, (timestamp, timestamp))
    cache_path = Path(importlib.util.cache_from_source(str(source_path)))
    cache_path.parent.mkdir(parents=True)
    py_compile.compile(str(source_path), cfile=str(cache_path), doraise=True)
    source_path.write_text(safe_source, encoding="utf-8")
    os.utime(source_path, (timestamp, timestamp))
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""

    sys.path.insert(0, str(repo))
    try:
        imported = importlib.import_module(module_name)
        assert imported.VALUE == "EVIL"
    finally:
        sys.path.remove(str(repo))
        sys.modules.pop(module_name, None)

    with pytest.raises(SourceSnapshotError, match="Ignored path outside"):
        GitSourceSnapshot(code_root=repo).capture()


def test_ignored_source_outside_runtime_roots_is_rejected(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    (repo / ".git" / "info" / "exclude").write_text(
        "ignored.py\n",
        encoding="utf-8",
    )
    (repo / "ignored.py").write_text(
        "raise RuntimeError('not reviewed')\n",
        encoding="utf-8",
    )

    with pytest.raises(
        SourceSnapshotError,
        match="Ignored path outside approved runtime roots",
    ):
        GitSourceSnapshot(code_root=repo).capture()


def test_assume_unchanged_cannot_hide_dirty_tracked_source(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _git(repo, "update-index", "--assume-unchanged", "--", "alpha.txt")
    (repo / "alpha.txt").write_bytes(b"hidden dirty content\n")
    assert _git(repo, "status", "--porcelain") == ""

    with pytest.raises(SourceSnapshotError, match="unsafe assume-unchanged"):
        GitSourceSnapshot(code_root=repo).capture()


def test_git_routing_and_config_environment_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repository(tmp_path)
    expected_commit = _git(repo, "rev-parse", "HEAD")
    invalid = tmp_path / "attacker-controlled-git-routing"
    monkeypatch.setenv("GIT_DIR", str(invalid / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(invalid))
    monkeypatch.setenv("GIT_INDEX_FILE", str(invalid / "index"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(invalid / "objects"))
    monkeypatch.setenv(
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        str(invalid / "alternate-objects"),
    )
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    snapshot = GitSourceSnapshot(code_root=repo).capture()

    assert snapshot.source_commit == expected_commit


def test_two_complete_captures_must_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repository(tmp_path)
    source = GitSourceSnapshot(code_root=repo)
    original = source._capture_once
    capture_count = 0

    def inconsistent_capture(repository_root: Path):
        nonlocal capture_count
        capture_count += 1
        captured = original(repository_root)
        if capture_count == 2:
            return replace(captured, manifest=captured.manifest + b"changed")
        return captured

    monkeypatch.setattr(source, "_capture_once", inconsistent_capture)

    with pytest.raises(SourceSnapshotError, match="changed while"):
        source.capture()


def test_change_after_second_manifest_is_caught_by_final_metadata_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repository(tmp_path)
    source = GitSourceSnapshot(code_root=repo)
    original = source._canonical_manifest
    manifest_count = 0

    def mutate_after_manifest(repository_root: Path, entries, **kwargs):
        nonlocal manifest_count
        manifest = original(repository_root, entries, **kwargs)
        manifest_count += 1
        if manifest_count == 2:
            (repo / "alpha.txt").write_bytes(b"changed after final manifest\n")
        return manifest

    monkeypatch.setattr(source, "_canonical_manifest", mutate_after_manifest)

    with pytest.raises(SourceSnapshotError, match="clean|changed"):
        source.capture()


def test_gitlink_is_rejected_even_when_submodule_is_clean(tmp_path: Path) -> None:
    parent = _repository(tmp_path / "parent")
    child = _repository(tmp_path / "child")
    _git(
        parent,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "--quiet",
        str(child),
        "vendor/child",
    )
    _git(
        parent,
        "config",
        "-f",
        ".gitmodules",
        "submodule.vendor/child.ignore",
        "all",
    )
    _git(parent, "add", "--", ".gitmodules")
    _git(parent, "commit", "--quiet", "-m", "add child submodule")
    with pytest.raises(SourceSnapshotError, match="Gitlinks are not permitted"):
        GitSourceSnapshot(code_root=parent).capture()


def test_tracked_symlink_is_rejected(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 'SAFE'\n", encoding="utf-8")
    link = repo / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")
    _git(repo, "add", "--", "linked.py")
    _git(repo, "commit", "--quiet", "-m", "add tracked symlink")

    with pytest.raises(SourceSnapshotError, match="Tracked symlinks"):
        GitSourceSnapshot(code_root=repo).capture()


@pytest.mark.parametrize("mutation", ["tracked", "ignored"])
def test_final_runtime_scan_mutation_is_caught_by_detailed_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    repo = _repository(tmp_path)
    (repo / ".git" / "info" / "exclude").write_text(
        "var/\n",
        encoding="utf-8",
    )
    source = GitSourceSnapshot(code_root=repo)
    original = source._validate_ignored_runtime_files
    scan_count = 0

    def mutate_after_final_scan(repository_root: Path) -> None:
        nonlocal scan_count
        original(repository_root)
        scan_count += 1
        if scan_count == 5:
            if mutation == "tracked":
                (repo / "alpha.txt").write_bytes(b"late tracked mutation\n")
            else:
                late = repo / "var" / "late.py"
                late.parent.mkdir(parents=True, exist_ok=True)
                late.write_text("VALUE = 'late'\n", encoding="utf-8")

    monkeypatch.setattr(source, "_validate_ignored_runtime_files", mutate_after_final_scan)

    with pytest.raises(SourceSnapshotError, match="clean|changed"):
        source.capture()


def test_explicit_code_root_works_when_process_root_is_not_the_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repository(tmp_path)
    runtime_root = tmp_path / "runtime-outside-git"
    runtime_root.mkdir()
    monkeypatch.chdir(runtime_root)

    snapshot = GitSourceSnapshot(code_root=repo / "nested").capture()

    assert snapshot.source_commit == _git(repo, "rev-parse", "HEAD")
    assert snapshot.dirty is False


def test_non_repository_code_root_is_rejected(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is required for source snapshot tests")
    code_root = tmp_path / "not-a-repository"
    code_root.mkdir()

    with pytest.raises(SourceSnapshotError, match="Git command failed"):
        GitSourceSnapshot(code_root=code_root).capture()
