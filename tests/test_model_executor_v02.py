from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import replace
from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.cli import build_parser
from company_os.model_executor import (
    CODEX_COMMAND,
    CliCodingExecutor,
    ExecutorOutcome,
    ExecutorRequest,
    build_instructions,
    codex_jsonl_cost,
    create_worktree,
    validate_worktree,
)

from .helpers import build_venture


FAKE_CLI = textwrap.dedent(
    """
    import json, pathlib, subprocess, sys, time
    mode = sys.argv[1]
    if mode == "edit":
        pathlib.Path("hello.py").write_text("def hello():\\n    return 'hi'\\n")
        print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1000,"cached_input_tokens":500,"output_tokens":100}}))
    elif mode == "break":
        pathlib.Path("hello.py").write_text("def hello():\\n    return 'bye'\\n")
    elif mode == "sleep":
        time.sleep(5)
    elif mode == "noop":
        print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}))
    elif mode == "denied":
        print(json.dumps({"type":"turn.completed","usage":{"input_tokens":2,"output_tokens":1}}))
        print("patch rejected: writing is blocked by read-only sandbox", file=sys.stderr)
    elif mode == "spawn-late":
        subprocess.Popen([sys.executable, "-c", "import pathlib,time; time.sleep(2.5); pathlib.Path('late.txt').write_text('late')"])
        time.sleep(5)
    """
)


def _repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "fake_cli.py").write_text(FAKE_CLI, encoding="utf-8")
    (repo / "hello.py").write_text("def hello():\n    return None\n", encoding="utf-8")
    (repo / "test_hello.py").write_text(
        "from hello import hello\n\ndef test_hello():\n    assert hello() == 'hi'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _request(workspace: Path, limit: int = 30) -> ExecutorRequest:
    return ExecutorRequest(
        work_order_id="WO-1",
        workspace=workspace,
        instructions="hello()가 hi를 반환하게 수정",
        time_limit_seconds=limit,
        cost_limit_usd=0.5,
        test_command=(
            sys.executable,
            "-c",
            "import test_hello; test_hello.test_hello()",
        ),
    )


def _executor(mode: str, *, rates=None) -> CliCodingExecutor:
    return CliCodingExecutor(
        [sys.executable, "fake_cli.py", mode],
        cost_parser=lambda output: codex_jsonl_cost(output, rates=rates),
    )


def test_worktree_edit_passes_and_main_is_untouched() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repo = _repo(root)
        workspace = create_worktree(repo, "wo/WO-1", root / "worktrees" / "WO-1")
        result = _executor(
            "edit",
            rates={"input": 1.0, "cached_input": 0.1, "output": 2.0},
        ).run(_request(workspace))
        assert result.ok and result.label == "DONE"
        assert result.changed_files == ["hello.py"]
        assert result.cost_usd == 0.00075 and not result.cost_unknown
        assert "return None" in (repo / "hello.py").read_text(encoding="utf-8")


def test_existing_worktree_must_belong_to_repository_and_branch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repo = _repo(root)
        workspace = create_worktree(
            repo,
            "wo/WO-existing",
            root / "worktrees" / "WO-existing",
        )
        assert validate_worktree(repo, "wo/WO-existing", workspace) == workspace.resolve()
        with pytest.raises(ValueError, match="branch"):
            validate_worktree(repo, "wo/WO-other", workspace)


def test_cli_exposes_bounded_model_run_entrypoint() -> None:
    parsed = build_parser().parse_args(
        [
            "work",
            "model-run",
            "WO-1",
            "--repository",
            "repo",
            "--worktree",
            "worktree",
            "--branch",
            "wo/WO-1",
            "--instructions-file",
            "instructions.txt",
            "--test-arg",
            "python",
            "--test-arg",
            "acceptance_test.py",
            "--idempotency-key",
            "model-run-1",
        ]
    )
    assert parsed.work_command == "model-run"
    assert parsed.test_arg == ["python", "acceptance_test.py"]
    assert parsed.reuse_worktree is False


def test_token_limit_is_enforced_independently_of_dollar_cost() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repo = _repo(root)
        workspace = create_worktree(
            repo,
            "wo/WO-token-limit",
            root / "worktrees" / "WO-token-limit",
        )
        result = _executor("edit").run(
            replace(_request(workspace), token_limit=10)
        )
        assert not result.ok
        assert result.label == "USAGE_LIMIT_EXCEEDED"
        assert result.usage_status == "EXCEEDED"
        assert result.usage_total_tokens == 1100
        assert result.token_accounting == "INPUT_PLUS_OUTPUT"
        assert result.token_limit_enforcement == "POST_EXECUTION_REJECTION"
        assert result.model_call_unit == "CODING_AGENT_CLI_PROCESS"


def test_timeout_kills_process_tree_before_late_side_effect() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repo = _repo(root)
        workspace = create_worktree(
            repo,
            "wo/WO-hard-timeout",
            root / "worktrees" / "WO-hard-timeout",
        )
        result = _executor("spawn-late").run(_request(workspace, limit=1))
        assert not result.ok and result.label == "TIMEOUT"
        assert result.time_limit_enforcement == "HARD_PROCESS_TREE_STOP"
        time.sleep(3)
        assert not (workspace / "late.txt").exists()


def test_failed_tests_no_changes_timeout_and_unknown_cost_are_explicit() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repo = _repo(root)
        for index, (mode, label, limit) in enumerate(
            (("break", "TESTS_FAILED", 30), ("noop", "NO_CHANGES", 30), ("sleep", "TIMEOUT", 1))
        ):
            workspace = create_worktree(
                repo,
                f"wo/WO-{index}",
                root / "worktrees" / f"WO-{index}",
            )
            result = _executor(mode).run(_request(workspace, limit=limit))
            assert not result.ok and result.label == label
            assert result.cost_unknown is True


def test_write_permission_denial_is_not_reported_as_no_changes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repo = _repo(root)
        workspace = create_worktree(
            repo,
            "wo/WO-denied",
            root / "worktrees" / "WO-denied",
        )
        result = _executor("denied").run(_request(workspace))
        assert not result.ok
        assert result.label == "WRITE_PERMISSION_DENIED"
        assert result.cost_unknown is True


def test_instruction_block_forbids_external_actions_and_test_edits() -> None:
    prompt = build_instructions("작업 본문", "python -m pytest -q")
    assert "외부 발송" in prompt
    assert "패키지 설치" in prompt
    assert "git commit" in prompt
    assert "테스트 파일의 기대값" in prompt


def test_codex_command_is_noninteractive_and_workspace_writable() -> None:
    assert CODEX_COMMAND[:6] == (
        "codex",
        "--ask-for-approval",
        "on-request",
        "--config",
        'windows.sandbox="elevated"',
        "exec",
    )
    assert CODEX_COMMAND[CODEX_COMMAND.index("--sandbox") + 1] == "workspace-write"
    assert "--ephemeral" in CODEX_COMMAND


def test_executor_environment_does_not_forward_api_keys(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    executor = _executor("noop")
    environment = executor.safe_environment()
    assert "OPENAI_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment


def test_windows_executor_environment_uses_accessible_shell_and_utf8(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            (
                r"C:\Users\test\AppData\Local\Microsoft\WindowsApps",
                r"C:\Program Files\Git\cmd",
            )
        ),
    )
    environment = _executor("noop").safe_environment()
    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["PYTHONUTF8"] == "1"
    if os.name == "nt":
        assert "WindowsApps" not in environment["PATH"]
        assert "WindowsPowerShell" in environment["PATH"]


def test_bounded_process_decodes_utf8_output_on_windows() -> None:
    completed = CliCodingExecutor._run_bounded(
        (
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write('한글'.encode('utf-8'))",
        ),
        cwd=Path.cwd(),
        environment=dict(os.environ),
        timeout=10,
    )
    assert completed.stdout == "한글"


def test_model_executor_outcome_is_recorded_as_run_event_and_evidence(
    tmp_path: Path,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repo = _repo(root)
        workspace = create_worktree(repo, "wo/WO-ledger", root / "worktrees" / "WO-ledger")
        outcome = _executor(
            "edit", rates={"input": 1.0, "cached_input": 0.1, "output": 2.0}
        ).run(_request(workspace))

    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "model-executor-ledger")
        run = company.record_model_execution(
            work_order.id,
            outcome,
            idempotency_key="model-executor-ledger-record",
        )
        evidence = company.evidence_for_run(run.id)
        assert run.outcome == "DONE" and run.cost_usd == outcome.cost_usd
        assert any(item.kind == "MODEL_EXECUTION" for item in evidence)
        assert "MODEL_EXECUTION_RECORDED" in {
            event["event_type"] for event in company.events()
        }


def test_unknown_and_over_limit_model_cost_have_distinct_outcomes(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "model-cost-policy")
        unknown = ExecutorOutcome(
            ok=True,
            label="DONE",
            cost_usd=None,
            cost_unknown=True,
            duration_seconds=0.1,
            usage={"input_tokens": 10, "output_tokens": 2},
            usage_status="WITHIN_LIMIT",
            usage_total_tokens=12,
        )
        unknown_run = company.record_model_execution(
            work_order.id,
            unknown,
            idempotency_key="model-cost-policy-unknown",
        )
        assert unknown_run.outcome == "DONE"
        assert unknown_run.status == "DONE"
        assert unknown_run.payload["cost_status"] == "UNAVAILABLE"
        assert unknown_run.payload["usage_status"] == "WITHIN_LIMIT"
        assert unknown_run.payload["budget_basis"] == "TIME_CALL_TOKEN"

        over = ExecutorOutcome(
            ok=True,
            label="DONE",
            cost_usd=work_order.cost_limit_usd + 0.01,
            cost_unknown=False,
            duration_seconds=0.1,
            usage={"input_tokens": 10, "output_tokens": 2},
            usage_status="WITHIN_LIMIT",
            usage_total_tokens=12,
        )
        over_run = company.record_model_execution(
            work_order.id,
            over,
            idempotency_key="model-cost-policy-over",
        )
        assert over_run.outcome == "COST_LIMIT_EXCEEDED"
        assert over_run.payload["cost_status"] == "EXCEEDED"


def test_permission_failure_remains_primary_when_cost_is_unknown(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "model-write-policy")
        denied = ExecutorOutcome(
            ok=False,
            label="WRITE_PERMISSION_DENIED",
            cost_usd=None,
            cost_unknown=True,
            duration_seconds=0.1,
            error="workspace is read-only",
        )
        run = company.record_model_execution(
            work_order.id,
            denied,
            idempotency_key="model-write-policy-denied",
        )
        assert run.outcome == "WRITE_PERMISSION_DENIED"
        assert run.payload["cost_status"] == "UNAVAILABLE"
