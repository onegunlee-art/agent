"""model_executor.py — 실제 모델 실행자 어댑터 (WorkOrder E).

전략: 모델 API를 직접 감싸는 대신, 이미 쓰는 코딩 에이전트 CLI(Claude Code 또는 Codex CLI)를
**비대화형(headless)** 으로 워크트리 안에서 실행한다. 이유:
  - 파일 편집·테스트 실행 루프를 새로 만들 필요가 없다.
  - 권한(허용 도구, 샌드박스)·턴 수·비용을 CLI 옵션으로 제한할 수 있다.
  - 실행자는 execution_lease.run_with_lease 의 executor 콜백으로 그대로 들어간다.

격리 원칙
  1. WorkOrder마다 git worktree + 브랜치를 새로 만든다 (create_worktree).
  2. CLI는 그 워크트리를 cwd로 실행되며, 끝난 뒤 변경 파일 목록을 git으로 기록한다.
  3. subprocess timeout = WorkOrder 제한 시간. 초과 시 프로세스를 죽이고 label=TIMEOUT.
  4. 비용은 CLI 출력에서 파싱한다. 파싱 불가(None)면 cost_unknown=True로 남기고,
     원장은 이를 상한 초과와 동일하게 취급할지 정책으로 정한다 (권장: 초과로 취급).
  5. 환경변수는 허용 목록만 넘긴다. API 키는 .env/비밀번호 관리자에서 환경변수로만 주입한다.

주의: 아래 CLI 플래그는 참조 예시다. 설치된 버전의 `--help`와 공식 문서로 확인한 뒤 쓴다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

# ---------------------------------------------------------------- 자료형
@dataclass
class ExecutorRequest:
    work_order_id: str
    workspace: Path                 # git worktree 경로
    instructions: str               # 작업지시서 본문 (범위·완료 기준·금지 포함)
    time_limit_seconds: int
    cost_limit_usd: float
    test_command: Sequence[str] = ("python", "-m", "pytest", "-q")


@dataclass
class ExecutorOutcome:
    ok: bool
    label: str                      # DONE | TIMEOUT | ERROR | TESTS_FAILED | NO_CHANGES
    cost_usd: Optional[float]
    cost_unknown: bool
    duration_s: float
    changed_files: list[str] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: Optional[str] = None


CostParser = Callable[[str], Optional[float]]


# ---------------------------------------------------------------- 비용 파서
def claude_code_cost(stdout: str) -> Optional[float]:
    """`claude -p ... --output-format json` 의 결과 JSON에서 total_cost_usd를 읽는다."""
    for chunk in (stdout.strip(), stdout.strip().splitlines()[-1] if stdout.strip() else ""):
        try:
            data = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and "total_cost_usd" in data:
            return float(data["total_cost_usd"])
    return None


def unknown_cost(_: str) -> Optional[float]:
    return None


# ---------------------------------------------------------------- CLI 명령 템플릿 (예시)
# {instructions} 자리에 작업지시서가 들어간다. 플래그는 설치 버전 문서로 반드시 확인.
CLAUDE_CODE_COMMAND = [
    "claude", "-p", "{instructions}",
    "--output-format", "json",
    "--max-turns", "30",
    "--permission-mode", "acceptEdits",
    "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(python -m pytest *)",
]
CODEX_COMMAND = [
    "codex", "exec", "--sandbox", "workspace-write", "{instructions}",
]

ENV_ALLOWLIST = ("PATH", "PATHEXT", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                 "SYSTEMROOT", "TEMP", "TMP", "LANG", "PYTHONIOENCODING",
                 "ANTHROPIC_API_KEY", "OPENAI_API_KEY")


# ---------------------------------------------------------------- 워크트리
def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout


def create_worktree(repo: Path, branch: str, path: Path, base: str = "main") -> Path:
    """repo에서 base로부터 새 브랜치를 만들어 path에 워크트리로 체크아웃한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", branch, str(path), base)
    return path


def remove_worktree(repo: Path, path: Path) -> None:
    subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=repo,
                   capture_output=True, text=True)


def changed_files(workspace: Path) -> list[str]:
    out = _git(workspace, "status", "--porcelain")
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------- 실행자
class CliCodingExecutor:
    """코딩 에이전트 CLI를 워크트리 안에서 제한 시간으로 돌리고, 끝나면 테스트를 실행한다."""

    def __init__(self, command: Sequence[str], cost_parser: CostParser,
                 env_allowlist: Sequence[str] = ENV_ALLOWLIST, run_tests: bool = True):
        self.command = list(command)
        self.cost_parser = cost_parser
        self.env_allowlist = tuple(env_allowlist)
        self.run_tests = run_tests

    def _env(self) -> dict:
        return {k: v for k, v in os.environ.items() if k in self.env_allowlist}

    def _resolve(self, cmd: list[str]) -> list[str]:
        exe = shutil.which(cmd[0])          # 윈도우의 claude.cmd / codex.cmd 대응
        if exe is None:
            raise FileNotFoundError(f"CLI를 찾을 수 없음: {cmd[0]}")
        return [exe, *cmd[1:]]

    def run(self, req: ExecutorRequest) -> ExecutorOutcome:
        started = time.monotonic()
        cmd = self._resolve([c.format(instructions=req.instructions) for c in self.command])
        try:
            proc = subprocess.run(cmd, cwd=req.workspace, capture_output=True, text=True,
                                  env=self._env(), timeout=req.time_limit_seconds)
        except subprocess.TimeoutExpired as exc:
            return ExecutorOutcome(
                ok=False, label="TIMEOUT", cost_usd=None, cost_unknown=True,
                duration_s=time.monotonic() - started,
                changed_files=changed_files(req.workspace),
                stdout_tail=(exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
                error=f"time limit {req.time_limit_seconds}s exceeded")
        except FileNotFoundError as exc:
            return ExecutorOutcome(ok=False, label="ERROR", cost_usd=None, cost_unknown=True,
                                   duration_s=0.0, error=str(exc))

        cost = self.cost_parser(proc.stdout)
        files = changed_files(req.workspace)
        outcome = ExecutorOutcome(
            ok=proc.returncode == 0, label="DONE" if proc.returncode == 0 else "ERROR",
            cost_usd=cost, cost_unknown=cost is None,
            duration_s=time.monotonic() - started, changed_files=files,
            stdout_tail=proc.stdout[-2000:], stderr_tail=proc.stderr[-2000:],
            error=None if proc.returncode == 0 else f"exit={proc.returncode}")

        if outcome.ok and not files:
            outcome.ok, outcome.label = False, "NO_CHANGES"
            outcome.error = "모델이 아무 파일도 바꾸지 않음"
            return outcome

        if outcome.ok and self.run_tests:
            remaining = max(30, int(req.time_limit_seconds - outcome.duration_s))
            try:
                t = subprocess.run(list(req.test_command), cwd=req.workspace, capture_output=True,
                                   text=True, env=self._env(), timeout=remaining)
            except subprocess.TimeoutExpired:
                outcome.ok, outcome.label, outcome.error = False, "TIMEOUT", "테스트 시간 초과"
                return outcome
            outcome.stdout_tail += "\n--- tests ---\n" + t.stdout[-2000:]
            if t.returncode != 0:
                outcome.ok, outcome.label = False, "TESTS_FAILED"
                outcome.error = f"tests exit={t.returncode}"
        outcome.duration_s = time.monotonic() - started
        return outcome


# ---------------------------------------------------------------- lease 연결
def to_exec_result(outcome: ExecutorOutcome, cost_limit_usd: float,
                   unknown_cost_exceeds_limit: bool = True):
    """execution_lease.ExecResult 로 변환. 비용 미상은 정책상 상한 초과로 취급(기본)."""
    from execution_lease import ExecResult  # 지연 import: 두 모듈을 독립적으로 쓸 수 있게

    cost = outcome.cost_usd
    if cost is None:
        cost = cost_limit_usd + 1.0 if unknown_cost_exceeds_limit else 0.0
    return ExecResult(ok=outcome.ok, cost_usd=cost, error=outcome.error, label=outcome.label)


def build_instructions(work_order_text: str, test_command: str,
                       workspace_note: str = "") -> str:
    """모델에게 넘길 지시문. 작업지시서 + 실행자 공통 금지 사항."""
    return f"""{work_order_text}

[실행 규칙 — 반드시 준수]
- 현재 폴더(워크트리) 안의 파일만 읽고 수정한다. 상위 폴더·다른 저장소는 건드리지 않는다.
- 외부 발송·게시·결제·네트워크 호출·패키지 설치를 하지 않는다.
- git commit/push/merge/branch 명령을 실행하지 않는다 (커밋은 실행자가 처리한다).
- .env, *.db, secrets/ 및 고객 자료 파일을 읽거나 출력하지 않는다.
- 완료 기준의 테스트가 통과할 때까지 수정하되, 테스트 파일의 기대값을 바꿔 통과시키지 않는다.
- 끝나면 바꾼 파일과 남은 문제를 5줄 이내로 요약한다.
테스트 명령: {test_command}
{workspace_note}""".strip()
