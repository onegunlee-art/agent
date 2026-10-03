"""Executable reproductions for the four reported release defects."""
from hashlib import sha256
import json
from pathlib import Path

import pytest

from company_os import cli
from company_os.source_snapshot import GitSourceSnapshot, SourceSnapshotError
from company_os.verifier import run_verifier, write_exact_text_verifier
from .test_first_principles_gate import valid_lite_contract, validate_contract
from .test_source_snapshot import _git, _repository


@pytest.mark.parametrize("expected,actual,status", [
    ("hello\r\n", b"hello\r\n", "PASS"),
    ("hello\n", b"hello\r\n", "FAIL"),
    ("hello\r", b"hello\r", "PASS"),
    ("hello\n", b"hello\n", "PASS"),
    ("hello", b"\xff", "FAIL"),
])
def test_exact_text_compares_and_hashes_the_same_bytes(tmp_path, expected, actual, status):
    spec = tmp_path / "verifier.json"
    write_exact_text_verifier(spec, artifact_relative_path="result.txt", expected_content=expected)
    (tmp_path / "result.txt").write_bytes(actual)
    result = run_verifier(spec, tmp_path)
    assert result.status == status
    assert result.artifact_sha256 == sha256(actual).hexdigest()


@pytest.mark.parametrize("field", ["observable_objective", "completion_criteria", "verification_method"])
@pytest.mark.parametrize("invalid", [True, False, 1, ["not a statement"], {"unrelated": "value"}])
def test_lite_required_statements_reject_non_text_placeholders(field, invalid):
    contract = valid_lite_contract()
    contract[field] = invalid
    assert not validate_contract(contract).passed


def test_cli_missing_input_is_a_structured_error(tmp_path, capsys):
    code = cli.main([
        "--root", str(tmp_path / "runtime"), "--db", str(tmp_path / "ledger.db"),
        "client", "init", "synthetic-probe", "--private-root", str(tmp_path / "private"),
        "--markers-file", str(tmp_path / "missing.json"),
        "--token-limit", "1000", "--cost-limit-usd", "1",
        "--idempotency-key", "missing-file-test",
    ])
    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "FileNotFoundError"


def test_cli_initialization_oserror_is_a_structured_error(tmp_path, capsys, monkeypatch):
    def denied(**kwargs):
        raise PermissionError("synthetic denied path")
    monkeypatch.setattr(cli, "CompanyOS", denied)
    assert cli.main(["--root", str(tmp_path), "status"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "PermissionError"


@pytest.mark.slow
def test_fsmonitor_cannot_hide_changed_worktree(tmp_path):
    repo = _repository(tmp_path)
    hook = repo / ".git" / "fake-fsmonitor"
    hook.write_bytes(b'#!/bin/sh\nprintf "token\\000"\n')
    hook.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", hook.as_posix())
    _git(repo, "config", "core.fsmonitorHookVersion", "2")
    _git(repo, "status", "--porcelain")
    _git(repo, "update-index", "--fsmonitor-valid", "alpha.txt")
    _git(repo, "status", "--porcelain")
    (repo / "alpha.txt").write_bytes(b"evil!\n")
    assert _git(repo, "status", "--porcelain") == ""  # reproduce lying status
    with pytest.raises(SourceSnapshotError):
        GitSourceSnapshot(code_root=repo).capture()


@pytest.mark.slow
@pytest.mark.parametrize("content", [b"changed\n", b"alpha\r\n", None])
def test_blob_comparison_does_not_trust_clean_metadata(tmp_path, monkeypatch, content):
    repo = _repository(tmp_path)
    source = GitSourceSnapshot(code_root=repo)
    metadata = source._repository_metadata(repo)
    if content is None:
        (repo / "alpha.txt").unlink()
    else:
        (repo / "alpha.txt").write_bytes(content)
    monkeypatch.setattr(source, "_repository_metadata", lambda root: metadata)
    with pytest.raises(SourceSnapshotError):
        source.capture()
