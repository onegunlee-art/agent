"""실제 CLI 없이 가짜 CLI(파이썬 스크립트)로 실행자 동작을 규정한다."""
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "reference"
sys.path.insert(0, str(ROOT))

import execution_lease as L  # noqa: E402
import model_executor as M  # noqa: E402

FAKE_CLI = textwrap.dedent("""
    import json, sys, time, pathlib
    mode = sys.argv[1]
    if mode == "edit":
        pathlib.Path("hello.py").write_text("def hello():\\n    return 'hi'\\n")
        print(json.dumps({"result": "done", "total_cost_usd": 0.05}))
    elif mode == "break":
        pathlib.Path("hello.py").write_text("def hello():\\n    return 'bye'\\n")
        print(json.dumps({"result": "done", "total_cost_usd": 0.05}))
    elif mode == "sleep":
        time.sleep(5)
    elif mode == "noop":
        print(json.dumps({"result": "nothing", "total_cost_usd": 0.01}))
""")

TEST_FILE = "from hello import hello\n\ndef test_hello():\n    assert hello() == 'hi'\n"


def _repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "fake_cli.py").write_text(FAKE_CLI)
    (repo / "test_hello.py").write_text(TEST_FILE)
    (repo / "hello.py").write_text("def hello():\n    return None\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _executor(mode: str) -> M.CliCodingExecutor:
    return M.CliCodingExecutor([sys.executable, "fake_cli.py", mode], M.claude_code_cost)


def _req(ws: Path, limit: int = 60) -> M.ExecutorRequest:
    return M.ExecutorRequest("WO-1", ws, "hello()가 'hi'를 돌려주게 고쳐라", limit, 1.0,
                             test_command=(sys.executable, "-c",
                                           "import sys; sys.path.insert(0,'.'); import test_hello; "
                                           "test_hello.test_hello()"))


def test_edit_in_worktree_passes_tests_and_leaves_main_untouched():
    with tempfile.TemporaryDirectory() as d:
        repo = _repo(Path(d))
        ws = M.create_worktree(repo, "wo/WO-1", Path(d) / "wt" / "WO-1")
        out = _executor("edit").run(_req(ws))
        assert out.ok and out.label == "DONE" and out.cost_usd == 0.05
        assert out.changed_files == ["hello.py"]
        assert "return None" in (repo / "hello.py").read_text()      # main 워크트리는 그대로


def test_tests_failed_is_reported():
    with tempfile.TemporaryDirectory() as d:
        repo = _repo(Path(d))
        ws = M.create_worktree(repo, "wo/WO-1", Path(d) / "wt" / "WO-1")
        out = _executor("break").run(_req(ws))
        assert not out.ok and out.label == "TESTS_FAILED"


def test_timeout_kills_process():
    with tempfile.TemporaryDirectory() as d:
        repo = _repo(Path(d))
        ws = M.create_worktree(repo, "wo/WO-1", Path(d) / "wt" / "WO-1")
        out = _executor("sleep").run(_req(ws, limit=1))
        assert not out.ok and out.label == "TIMEOUT" and out.cost_unknown


def test_no_changes_is_not_success():
    with tempfile.TemporaryDirectory() as d:
        repo = _repo(Path(d))
        ws = M.create_worktree(repo, "wo/WO-1", Path(d) / "wt" / "WO-1")
        out = _executor("noop").run(_req(ws))
        assert not out.ok and out.label == "NO_CHANGES"


def test_unknown_cost_is_treated_as_over_limit_by_default():
    out = M.ExecutorOutcome(ok=True, label="DONE", cost_usd=None, cost_unknown=True, duration_s=1)
    res = M.to_exec_result(out, cost_limit_usd=1.0)
    assert res.cost_usd > 1.0


def test_executor_plugs_into_lease_loop():
    with tempfile.TemporaryDirectory() as d:
        repo = _repo(Path(d))
        ws = M.create_worktree(repo, "wo/WO-1", Path(d) / "wt" / "WO-1")
        conn = L.connect(":memory:")
        L.register_work_order(conn, "WO-1", 60, 1.0)
        ex = _executor("edit")
        outcome = L.run_with_lease(
            conn, "WO-1",
            lambda lease: M.to_exec_result(ex.run(_req(ws)), lease.cost_limit_usd),
            now_fn=lambda: 100)
        assert outcome == "DONE" and L.get_status(conn, "WO-1") == L.DONE
        run = conn.execute("SELECT cost_usd FROM execution_run").fetchone()
        assert run["cost_usd"] == 0.05
