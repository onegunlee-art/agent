from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest

from company_os.application import CompanyOS
from company_os.errors import ConflictError, ValidationError
from company_os.fakes import FakeExecutor
from company_os.model_executor import ExecutorOutcome
from company_os.source_snapshot import SourceSnapshot, SourceSnapshotError
from .helpers import CleanSourceSnapshotter, build_venture


@pytest.fixture
def job_setup(tmp_path, monkeypatch):
    repo = tmp_path / "product"
    repo.mkdir()
    (repo / "writer.py").write_text("VALUE = 'wrong'\n", encoding="utf-8")
    (repo / "test_writer.py").write_text(
        "from writer import VALUE\ndef test_render():\n    assert isinstance(VALUE, str)\n", encoding="utf-8")
    for args in (("init", "-b", "wo/synthetic"), ("add", "."),
                 ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=20)
    # Keep orchestration tests fast. Actual blob/checkout reproduction is covered
    # by source_snapshot tests and the separate live CLI acceptance demo.
    from company_os import application, automation, headless_reviewer
    class ProductSnapshot:
        def __init__(self, *, code_root):
            assert Path(code_root).resolve() == repo.resolve()
        def capture(self):
            status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                    cwd=repo, capture_output=True, text=True, timeout=20, check=True).stdout
            if status:
                raise SourceSnapshotError("dirty product")
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                    capture_output=True, text=True, timeout=20, check=True).stdout.strip()
            digest = sha256((repo / "writer.py").read_bytes() + (repo / "test_writer.py").read_bytes()).hexdigest()
            return SourceSnapshot(commit, "d" * 40, digest, False)
    for module in (application, automation, headless_reviewer):
        monkeypatch.setattr(module, "GitSourceSnapshot", ProductSnapshot)
    company = CompanyOS(tmp_path / "runtime", source_snapshotter=CleanSourceSnapshotter())
    company.initialize()
    _, _, _, work = build_venture(company, "automation")
    company.execute_work_order(work.id, executor=FakeExecutor(), idempotency_key="initial")
    plan = {"schema_version": 1, "work_order_id": work.id, "repository": str(repo),
            "allowed_files": ["writer.py"], "test_command": [sys.executable, "-B", "-m", "pytest", "-q"],
            "claude_executable": sys.executable, "max_repairs": 2}
    yield company, work, repo, plan
    company.close()


def install_reviewer(monkeypatch, company, *, fixed_status=None, force_changes=False):
    from company_os import headless_reviewer
    def invoke(command, *, cwd, environment, timeout, input_text=None):
        assert not company.store.connection.in_transaction
        if "--output-format" not in command:
            return subprocess.CompletedProcess(command, 0, "1 passed", "")
        if fixed_status:
            return subprocess.CompletedProcess(command, 1, json.dumps({"is_error": True, "result": fixed_status}), "")
        request = json.loads((cwd / "review_request.json").read_text(encoding="utf-8"))
        verdict = "PASS" if not force_changes and "correct" in (cwd / "source/writer.py").read_text() else "CHANGES_REQUIRED"
        result = {"schema_version": 2, "source": "headless_claude", "verdict": verdict,
                  "review_request_id": request["review_request_id"], "review_request_hash": request["review_request_hash"],
                  "reviewed_commit": request["source_commit"], "reviewed_tree_sha256": request["source_tree_sha256"],
                  "findings": [], "required_changes": [] if verdict == "PASS" else [{"id": "C1", "description": "Set VALUE to correct"}]}
        return subprocess.CompletedProcess(command, 0, json.dumps({"type": "result", "subtype": "success", "is_error": False, "structured_output": result,
                                            "usage": {"input_tokens": 60, "output_tokens": 10}}), "")
    monkeypatch.setattr(headless_reviewer, "_run_process", invoke)


class RepairExecutor:
    name = "synthetic_repair_not_real_model"
    calls = 0
    def __init__(self, company, *, violate=False, callback=None):
        self.company, self.violate, self.callback = company, violate, callback
    def run(self, request):
        assert not self.company.store.connection.in_transaction
        self.calls += 1
        (request.workspace / "writer.py").write_text("VALUE = 'correct'\n", encoding="utf-8")
        if self.violate:
            (request.workspace / "test_writer.py").write_text("def test_render(): assert True\n", encoding="utf-8")
        if self.callback:
            self.callback()
        return ExecutorOutcome(ok=True, label="DONE", cost_usd=None, cost_unknown=True,
                               duration_seconds=0.1, usage={"input_tokens": 80, "output_tokens": 20},
                               usage_status="WITHIN_LIMIT", usage_total_tokens=100)


def test_tick_repairs_tests_records_manifest_and_rereviews_after_restart(job_setup, monkeypatch):
    from company_os.automation import enqueue, tick, jobs
    company, work, repo, plan = job_setup
    install_reviewer(monkeypatch, company)
    job = enqueue(company, plan, idempotency_key="job")
    assert enqueue(company, plan, idempotency_key="job")["id"] == job["id"]
    assert tick(company)["status"] == "REPAIR"
    company.close()
    company.initialize()
    executor = RepairExecutor(company)
    repaired = tick(company, executor=executor)
    assert repaired["status"] == "REVIEW", repaired["last_receipt"]
    assert executor.calls == 1
    assert tick(company)["status"] == "READY_FOR_CEO"
    assert tick(company)["status"] == "IDLE"
    assert company.work_order(work.id).status == "COMPLETED"
    state = jobs(company)[0]
    assert state["repair_count"] == 1
    assert state["total_tokens"] == 240
    assert state["cost_usd"] is None
    assert state["model_calls"] == 3
    evidence = company.store.query_all("SELECT * FROM evidence WHERE work_order_id=?", (work.id,))
    assert {row["kind"] for row in evidence} >= {"TEST_RESULT", "RUN_REPRODUCIBILITY"}
    model = next(run for run in company.runs_for_work_order(work.id)
                 if run.payload.get("production_execution"))
    payload = json.loads(company.store.get_row("runs", model.id)["payload_json"])
    assert model.execution_id and model.fence_token
    assert payload["production_execution"] and not payload["diagnostic_only"]
    assert not subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True,
                              text=True, timeout=20).stdout


def test_tick_protects_test_expectations_and_preserves_failure_evidence(job_setup, monkeypatch):
    from company_os.automation import enqueue, tick
    company, work, _, plan = job_setup
    install_reviewer(monkeypatch, company)
    enqueue(company, plan, idempotency_key="job")
    tick(company)
    assert tick(company, executor=RepairExecutor(company, violate=True))["status"] == "NEEDS_ATTENTION"
    assert company.work_order(work.id).status == "REPAIR_REQUIRED"
    assert tick(company)["status"] == "IDLE"
    assert any(e["event_type"] == "AUTOMATION_JOB_FINISHED_STEP" for e in company.events())


def test_quota_wait_does_not_hot_retry_or_turn_into_failure(job_setup, monkeypatch):
    from company_os.automation import enqueue, tick, jobs
    company, work, _, plan = job_setup
    install_reviewer(monkeypatch, company, fixed_status="rate limit exceeded")
    enqueue(company, plan, idempotency_key="job")
    assert tick(company)["status"] == "QUOTA_WAIT"
    assert tick(company)["status"] == "IDLE"
    assert jobs(company)[0]["provider_reset_at"] is None
    assert company.work_order(work.id).status == "WAITING_FOR_OPUS"


def test_daily_limit_is_disabled_but_usage_still_counted(job_setup, monkeypatch):
    from company_os.automation import enqueue, tick, usage
    company, _, _, plan = job_setup
    install_reviewer(monkeypatch, company)
    enqueue(company, plan, idempotency_key="job")
    tick(company)
    assert usage(company)["daily_token_limit"] is None
    assert usage(company)["tokens"] == 70
    assert usage(company)["calls"] == 1
    assert usage(company)["cost_usd"] is None


def test_stop_and_expired_worker_never_execute_or_replay_model(job_setup, monkeypatch):
    from company_os.automation import enqueue, tick, jobs
    company, _, _, plan = job_setup
    enqueue(company, plan, idempotency_key="job")
    company.stop()
    assert tick(company)["status"] == "STOPPED"
    company.resume()
    job = jobs(company)[0]
    with company.store.transaction() as connection:
        job.update(status="RUNNING", phase="REPAIR", expires_at=0, execution_id="dead-worker")
        company.store.set_global_state("automation:job:" + job["id"], job, connection=connection)
    assert tick(company)["status"] == "NEEDS_ATTENTION"
    assert tick(company)["status"] == "IDLE"


def test_enqueue_rejects_main_branch_and_test_allowlist(job_setup):
    from company_os.automation import enqueue
    company, _, repo, plan = job_setup
    with pytest.raises(ValidationError, match="test"):
        enqueue(company, {**plan, "allowed_files": ["test_writer.py"]}, idempotency_key="bad-test")
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo, check=True, capture_output=True, timeout=20)
    with pytest.raises(ValidationError, match="wo/"):
        enqueue(company, plan, idempotency_key="bad-branch")


def test_daily_budget_reserves_before_any_external_call(job_setup, monkeypatch):
    from company_os.automation import configure_daily_limits, enqueue, tick, usage
    company, _, _, plan = job_setup
    install_reviewer(monkeypatch, company)
    enqueue(company, plan, idempotency_key="job")
    configure_daily_limits(company, tokens=10, calls=1)
    assert tick(company)["status"] == "DAILY_BUDGET_WAIT"
    assert usage(company)["calls"] == 0
    assert not any(e["event_type"] == "HEADLESS_REVIEW_STARTED" for e in company.events())


def test_concurrent_tick_is_busy_and_old_worker_cannot_finalize(job_setup, monkeypatch):
    from company_os.automation import enqueue, tick, jobs
    company, _, _, plan = job_setup
    install_reviewer(monkeypatch, company)
    enqueue(company, plan, idempotency_key="job")
    tick(company)
    def supersede():
        assert tick(company)["status"] == "BUSY"
        current = jobs(company)[0]
        current.update(status="NEEDS_ATTENTION", execution_id="new-owner", fence_token=current["fence_token"] + 1)
        company.store.set_global_state("automation:job:" + current["id"], current)
    result = tick(company, executor=RepairExecutor(company, callback=supersede))
    assert result["status"] == "STALE_RESULT_REJECTED"
    assert jobs(company)[0]["execution_id"] == "new-owner"
    assert not any("Repair C1" in subprocess.run(["git", "log", "--oneline"],
                          cwd=Path(plan["repository"]), capture_output=True, text=True, timeout=20).stdout for _ in [0])


def test_queued_protected_file_change_is_rejected_before_review(job_setup, monkeypatch):
    from company_os.automation import enqueue, tick
    company, _, repo, plan = job_setup
    install_reviewer(monkeypatch, company)
    enqueue(company, plan, idempotency_key="job")
    (repo / "test_writer.py").write_text("def test_render(): assert True\n", encoding="utf-8")
    result = tick(company)
    assert result["status"] == "NEEDS_ATTENTION"
    assert not any(e["event_type"] == "HEADLESS_REVIEW_STARTED" for e in company.events())


def test_dashboard_shows_queue_without_exposing_plan_or_writing(job_setup):
    from company_os.automation import enqueue
    from company_os.dashboard import read_dashboard, _render_page
    company, work, _, plan = job_setup
    job = enqueue(company, plan, idempotency_key="job")
    before = len(company.events())
    snapshot = read_dashboard(company.db_path)
    assert snapshot["automation_jobs"][0]["id"] == job["id"]
    assert "plan" not in snapshot["automation_jobs"][0]
    page = _render_page(snapshot, "synthetic-token", "http://127.0.0.1:8765/")
    assert "수동 실행 대기열" in page and job["id"] in page
    assert len(company.events()) == before


def test_one_manual_run_completes_bounded_loop_without_scheduler(job_setup, monkeypatch):
    from company_os.automation import enqueue, run_queue
    company, work, _, plan = job_setup
    install_reviewer(monkeypatch, company)
    enqueue(company, plan, idempotency_key="job")
    steps = run_queue(company, executor=RepairExecutor(company))
    assert [step["status"] for step in steps] == ["REPAIR", "REVIEW", "READY_FOR_CEO", "IDLE"]
    assert company.work_order(work.id).status == "COMPLETED"


def test_executor_cleanup_has_a_timeout_even_after_process_tree_kill(monkeypatch, tmp_path):
    from company_os import model_executor
    class Process:
        pid = 12345
        calls = 0
        def communicate(self, *, timeout=None):
            assert timeout is not None, "cleanup must never wait forever"
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("synthetic", timeout)
            return "", ""
    monkeypatch.setattr(model_executor.subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr(model_executor.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(model_executor.os, "killpg", lambda *a: None, raising=False)
    with pytest.raises(subprocess.TimeoutExpired):
        model_executor.CliCodingExecutor._run_bounded(["synthetic"], cwd=tmp_path, environment={}, timeout=1)


def test_repair_ceiling_stops_without_another_model_call(job_setup, monkeypatch):
    from company_os.automation import enqueue, run_queue
    company, work, _, plan = job_setup
    install_reviewer(monkeypatch, company, force_changes=True)
    enqueue(company, {**plan, "max_repairs": 1}, idempotency_key="job")
    executor = RepairExecutor(company)
    steps = run_queue(company, executor=executor)
    assert [step["status"] for step in steps] == ["REPAIR", "REVIEW", "NEEDS_ATTENTION"]
    assert steps[-1]["last_receipt"]["reason"] == "REPAIR_LIMIT_REACHED"
    assert executor.calls == 1
    assert company.work_order(work.id).status == "REPAIR_REQUIRED"
