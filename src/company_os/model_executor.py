"""Bounded coding-agent CLI executor for isolated Git worktrees."""

from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Callable, Mapping, Sequence


@dataclass(frozen=True)
class ExecutorRequest:
    work_order_id: str
    workspace: Path
    instructions: str
    time_limit_seconds: int
    cost_limit_usd: float
    test_command: Sequence[str]
    model_call_limit: int = 1
    token_limit: int = 250_000


@dataclass(frozen=True)
class WorkspaceIdentity:
    branch: str
    head_commit: str
    sparse_checkout_patterns: tuple[str, ...]


@dataclass
class ExecutorOutcome:
    ok: bool
    label: str
    cost_usd: float | None
    cost_unknown: bool
    duration_seconds: float
    changed_files: list[str] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    usage_status: str = "UNAVAILABLE"
    usage_total_tokens: int = 0
    model_calls: int = 1
    model_call_unit: str = "CODING_AGENT_CLI_PROCESS"
    token_accounting: str = "INPUT_PLUS_OUTPUT"
    token_limit_enforcement: str = "POST_EXECUTION_REJECTION"
    time_limit_enforcement: str = "HARD_PROCESS_TREE_STOP"
    workspace_branch: str = ""
    workspace_head: str = ""
    sparse_checkout_patterns: list[str] = field(default_factory=list)
    workspace_diff: str = ""
    changed_file_sha256: dict[str, str] = field(default_factory=dict)


CostParser = Callable[[str], tuple[float | None, dict[str, int]]]

CODEX_COMMAND = (
    "codex",
    "--ask-for-approval",
    "on-request",
    "--config",
    'windows.sandbox="elevated"',
    "exec",
    "--sandbox",
    "workspace-write",
    "--ephemeral",
    "--json",
    "--ignore-user-config",
    "{instructions}",
)

SAFE_ENVIRONMENT = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "TEMP",
    "TMP",
    "LANG",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
)

_BRANCH = re.compile(r"^wo/[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_GIT_TIMEOUT_SECONDS = 30


def codex_jsonl_cost(
    stdout: str,
    *,
    rates: Mapping[str, float] | None = None,
) -> tuple[float | None, dict[str, int]]:
    """Parse Codex JSONL usage and optionally estimate USD from configured rates.

    Rates are USD per million tokens. They are configuration, not hard-coded
    pricing, because model prices and ChatGPT-managed billing can differ.
    """

    usage: dict[str, int] = {}
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if payload.get("type") != "turn.completed" or not isinstance(
            payload.get("usage"), dict
        ):
            continue
        raw = payload["usage"]
        usage = {
            "input_tokens": int(raw.get("input_tokens", 0)),
            "cached_input_tokens": int(raw.get("cached_input_tokens", 0)),
            "output_tokens": int(raw.get("output_tokens", 0)),
            "reasoning_output_tokens": int(raw.get("reasoning_output_tokens", 0)),
        }
    if not usage or rates is None:
        return None, usage
    required = {"input", "cached_input", "output"}
    if not required.issubset(rates):
        return None, usage
    cached = min(usage["cached_input_tokens"], usage["input_tokens"])
    uncached = usage["input_tokens"] - cached
    output = usage["output_tokens"]
    cost = (
        uncached * float(rates["input"])
        + cached * float(rates["cached_input"])
        + output * float(rates["output"])
    ) / 1_000_000
    return round(cost, 8), usage


def _git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    return completed.stdout


def capture_workspace_identity(workspace: Path) -> WorkspaceIdentity:
    """Capture the exact Git view presented to a coding-agent process."""

    workspace = workspace.resolve()
    branch = _git(workspace, "branch", "--show-current").strip()
    head_commit = _git(workspace, "rev-parse", "HEAD").strip()
    sparse_enabled = subprocess.run(
        ["git", "config", "--bool", "core.sparseCheckout"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    ).stdout.strip().casefold() == "true"
    patterns: tuple[str, ...] = ()
    if sparse_enabled:
        completed = subprocess.run(
            ["git", "sparse-checkout", "list"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "sparse checkout is enabled but its patterns cannot be read"
            )
        patterns = tuple(
            line.strip() for line in completed.stdout.splitlines() if line.strip()
        )
    return WorkspaceIdentity(branch, head_commit, patterns)


def create_worktree(
    repository: Path,
    branch: str,
    destination: Path,
    *,
    base: str = "HEAD",
) -> Path:
    repository = repository.resolve()
    destination = destination.resolve()
    if not _BRANCH.fullmatch(branch):
        raise ValueError("worktree branch must use the bounded wo/<id> form")
    if destination.exists():
        raise FileExistsError(f"worktree destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "-b", branch, str(destination), base)
    return destination


def validate_worktree(repository: Path, branch: str, destination: Path) -> Path:
    repository = repository.resolve()
    destination = destination.resolve()
    if not _BRANCH.fullmatch(branch):
        raise ValueError("worktree branch must use the bounded wo/<id> form")
    if not destination.is_dir():
        raise ValueError(f"worktree does not exist: {destination}")
    observed_root = Path(_git(destination, "rev-parse", "--show-toplevel").strip()).resolve()
    if observed_root != destination:
        raise ValueError("worktree path is not the Git worktree root")
    observed_branch = _git(destination, "branch", "--show-current").strip()
    if observed_branch != branch:
        raise ValueError(
            f"worktree branch mismatch: expected {branch}, observed {observed_branch}"
        )
    common = Path(_git(destination, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = (destination / common).resolve()
    else:
        common = common.resolve()
    expected_common = (repository / ".git").resolve()
    if common != expected_common:
        raise ValueError("worktree does not belong to the requested repository")
    return destination


def changed_files(workspace: Path, base_commit: str | None = None) -> list[str]:
    output = _git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
    paths: set[str] = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        candidate = line[3:]
        if " -> " in candidate:
            candidate = candidate.split(" -> ", 1)[1]
        paths.add(candidate.strip().strip('"'))
    if base_commit is not None:
        committed_or_staged = _git(
            workspace,
            "diff",
            "--name-only",
            "--no-ext-diff",
            base_commit,
            "--",
        )
        paths.update(
            line.strip() for line in committed_or_staged.splitlines() if line.strip()
        )
    return sorted(paths)


def capture_workspace_diff(workspace: Path, base_commit: str = "HEAD") -> str:
    # Diff from the captured starting commit so staged changes cannot disappear
    # from the audit record. Untracked files are represented by their full-file
    # SHA-256 entries in ``changed_file_sha256``.
    return _git(
        workspace,
        "diff",
        "--binary",
        "--no-ext-diff",
        base_commit,
        "--",
    )


def changed_file_fingerprints(
    workspace: Path,
    paths: Sequence[str],
) -> dict[str, str]:
    workspace = workspace.resolve()
    fingerprints: dict[str, str] = {}
    for relative in paths:
        candidate = workspace / relative
        resolved = candidate.resolve(strict=False)
        if workspace != resolved and workspace not in resolved.parents:
            raise ValueError(f"changed file escapes worktree: {relative}")
        if candidate.is_symlink():
            fingerprints[relative] = sha256(
                os.readlink(candidate).encode("utf-8")
            ).hexdigest()
        elif candidate.is_file():
            digest = sha256()
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            fingerprints[relative] = digest.hexdigest()
        elif not candidate.exists():
            fingerprints[relative] = "DELETED"
        else:
            fingerprints[relative] = "NON_FILE"
    return fingerprints


def build_instructions(
    work_order_text: str,
    test_command: str,
    workspace_note: str = "",
) -> str:
    return f"""{work_order_text}

[실행 규칙 — 반드시 준수]
- 현재 Git 워크트리 안의 파일만 읽고 수정한다. 상위 폴더와 다른 저장소는 건드리지 않는다.
- 외부 발송·게시·결제·네트워크 호출·패키지 설치를 하지 않는다.
- git commit/push/merge/branch 명령을 실행하지 않는다. 버전 확정은 Company OS가 담당한다.
- .env, *.db, secrets/와 다른 고객 자료를 읽거나 출력하지 않는다.
- 완료 기준의 테스트를 실행하되 테스트 파일의 기대값을 바꿔 통과시키지 않는다.
- 범위 밖 문제는 수정하지 말고 마지막 요약에만 기록한다.
- 끝나면 바꾼 파일과 남은 문제를 다섯 줄 이내로 요약한다.
테스트 명령: {test_command}
{workspace_note}""".strip()


class CliCodingExecutor:
    """Run one configured coding-agent command and its acceptance test."""

    def __init__(
        self,
        command: Sequence[str] = CODEX_COMMAND,
        cost_parser: CostParser | None = None,
        *,
        environment_allowlist: Sequence[str] = SAFE_ENVIRONMENT,
        run_tests: bool = True,
    ) -> None:
        self.command = list(command)
        self.cost_parser = cost_parser or (lambda output: codex_jsonl_cost(output))
        self.environment_allowlist = tuple(environment_allowlist)
        self.run_tests = run_tests

    def safe_environment(self) -> dict[str, str]:
        environment = {
            name: value
            for name, value in os.environ.items()
            if name in self.environment_allowlist
        }
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        environment.setdefault("LANG", "C.UTF-8")
        if os.name == "nt":
            path_entries = [
                entry
                for entry in environment.get("PATH", "").split(os.pathsep)
                if entry and "windowsapps" not in entry.casefold()
            ]
            windows_root = environment.get("SYSTEMROOT", r"C:\Windows")
            shell_path = str(
                Path(windows_root) / "System32" / "WindowsPowerShell" / "v1.0"
            )
            environment["PATH"] = os.pathsep.join((shell_path, *path_entries))
        return environment

    @staticmethod
    def _resolve(command: Sequence[str]) -> list[str]:
        executable = shutil.which(command[0])
        if executable is None:
            raise FileNotFoundError(f"coding-agent CLI not found: {command[0]}")
        return [executable, *command[1:]]

    @staticmethod
    def _run_bounded(
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        creation_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        )
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(environment),
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            stdout, stderr = process.communicate()
            exc.stdout = stdout
            exc.stderr = stderr
            raise
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def run(self, request: ExecutorRequest) -> ExecutorOutcome:
        started = time.monotonic()
        if request.time_limit_seconds <= 0:
            raise ValueError("time limit must be positive")
        if request.model_call_limit < 1:
            raise ValueError("model call limit must be positive")
        if request.token_limit <= 0:
            raise ValueError("token limit must be positive")
        identity = capture_workspace_identity(request.workspace)
        identity_fields = {
            "workspace_branch": identity.branch,
            "workspace_head": identity.head_commit,
            "sparse_checkout_patterns": list(identity.sparse_checkout_patterns),
        }
        command = self._resolve(
            [part.format(instructions=request.instructions) for part in self.command]
        )
        environment = self.safe_environment()
        try:
            completed = self._run_bounded(
                command,
                cwd=request.workspace,
                environment=environment,
                timeout=request.time_limit_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            files = changed_files(request.workspace, identity.head_commit)
            return ExecutorOutcome(
                ok=False,
                label="TIMEOUT",
                cost_usd=None,
                cost_unknown=True,
                duration_seconds=time.monotonic() - started,
                changed_files=files,
                stdout_tail=(exc.stdout or "")[-4000:],
                stderr_tail=(exc.stderr or "")[-4000:],
                error=f"time limit {request.time_limit_seconds}s exceeded",
                workspace_diff=capture_workspace_diff(
                    request.workspace, identity.head_commit
                ),
                changed_file_sha256=changed_file_fingerprints(
                    request.workspace, files
                ),
                **identity_fields,
            )
        except FileNotFoundError as exc:
            return ExecutorOutcome(
                ok=False,
                label="ERROR",
                cost_usd=None,
                cost_unknown=True,
                duration_seconds=time.monotonic() - started,
                error=str(exc),
                **identity_fields,
            )

        cost, usage = self.cost_parser(completed.stdout)
        usage_total_tokens = int(usage.get("input_tokens", 0)) + int(
            usage.get("output_tokens", 0)
        )
        usage_status = (
            "UNAVAILABLE"
            if not usage
            else (
                "EXCEEDED"
                if usage_total_tokens > request.token_limit
                else "WITHIN_LIMIT"
            )
        )
        files = changed_files(request.workspace, identity.head_commit)
        workspace_diff = capture_workspace_diff(request.workspace, identity.head_commit)
        file_fingerprints = changed_file_fingerprints(request.workspace, files)
        current_head = _git(request.workspace, "rev-parse", "HEAD").strip()
        outcome = ExecutorOutcome(
            ok=completed.returncode == 0,
            label="DONE" if completed.returncode == 0 else "ERROR",
            cost_usd=cost,
            cost_unknown=cost is None,
            duration_seconds=time.monotonic() - started,
            changed_files=files,
            stdout_tail=completed.stdout[-4000:],
            stderr_tail=completed.stderr[-4000:],
            error=None if completed.returncode == 0 else f"exit={completed.returncode}",
            usage=usage,
            usage_status=usage_status,
            usage_total_tokens=usage_total_tokens,
            model_calls=1,
            workspace_diff=workspace_diff,
            changed_file_sha256=file_fingerprints,
            **identity_fields,
        )
        if current_head != identity.head_commit:
            outcome.ok = False
            outcome.label = "GIT_HISTORY_MUTATED"
            outcome.error = (
                "coding agent changed Git history; commits are owned by Company OS"
            )
            return outcome
        if outcome.ok and not files:
            outcome.ok = False
            combined_output = f"{completed.stdout}\n{completed.stderr}".casefold()
            permission_markers = (
                "read-only sandbox",
                "writing is blocked",
                "write permission denied",
                "workspace is read-only",
            )
            if any(marker in combined_output for marker in permission_markers):
                outcome.label = "WRITE_PERMISSION_DENIED"
                outcome.error = "coding agent workspace was not writable"
            else:
                outcome.label = "NO_CHANGES"
                outcome.error = "coding agent changed no files"
            return outcome
        if not outcome.ok or not self.run_tests:
            return outcome

        remaining = request.time_limit_seconds - outcome.duration_seconds
        if remaining <= 0:
            outcome.ok = False
            outcome.label = "TIMEOUT"
            outcome.error = "no time remained for acceptance tests"
            return outcome
        try:
            tests = self._run_bounded(
                request.test_command,
                cwd=request.workspace,
                environment=environment,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            outcome.ok = False
            outcome.label = "TIMEOUT"
            outcome.error = "acceptance tests timed out"
            return outcome
        outcome.stdout_tail += "\n--- acceptance tests ---\n" + tests.stdout[-4000:]
        outcome.stderr_tail += "\n--- acceptance tests ---\n" + tests.stderr[-4000:]
        outcome.duration_seconds = time.monotonic() - started
        if tests.returncode != 0:
            outcome.ok = False
            outcome.label = "TESTS_FAILED"
            outcome.error = f"acceptance tests exit={tests.returncode}"
            return outcome
        if usage_status != "WITHIN_LIMIT":
            outcome.ok = False
            if usage_status == "EXCEEDED":
                outcome.label = "USAGE_LIMIT_EXCEEDED"
                outcome.error = "coding agent token limit exceeded"
            else:
                outcome.label = "USAGE_UNAVAILABLE"
                outcome.error = "coding agent usage could not be measured"
        return outcome
