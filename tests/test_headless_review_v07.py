from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from company_os.application import CompanyOS
from company_os.errors import ConflictError, ValidationError
from company_os.fakes import FakeExecutor
from company_os.source_snapshot import GitSourceSnapshot
from company_os.utils import atomic_write_json
from .helpers import CleanSourceSnapshotter, build_venture


@pytest.fixture
def setup_review(tmp_path):
    repo = tmp_path / "product"
    repo.mkdir()
    (repo / "app.py").write_text("ANSWER = 42\n", encoding="utf-8")
    for args in (("init",), ("add", "."),
                 ("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                  "commit", "-m", "synthetic product")):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True, timeout=20)
    company = CompanyOS(tmp_path / "runtime", source_snapshotter=CleanSourceSnapshotter())
    company.initialize()
    _, _, _, work = build_venture(company, "headless")
    company.execute_work_order(work.id, executor=FakeExecutor(), idempotency_key="run")
    yield company, work, repo
    company.close()


def install_process_stub(monkeypatch, company, *, verdict="PASS", error=None, callback=None):
    from company_os import headless_reviewer as module
    calls = []

    def invoke(command, *, cwd, environment, timeout, input_text=None):
        assert not company.store.connection.in_transaction
        calls.append(command)
        if "--output-format" not in command:
            return subprocess.CompletedProcess(command, 0, "1 passed\n", "")
        request = json.loads((cwd / "review_request.json").read_text(encoding="utf-8"))
        assert request["source_commit"] != "a" * 40
        assert (cwd / "source" / "app.py").is_file()
        if callback:
            callback(request)
        payload = {
            "schema_version": 2,
            "review_request_id": request["review_request_id"],
            "review_request_hash": request["review_request_hash"],
            "reviewed_commit": request["source_commit"],
            "reviewed_tree_sha256": request["source_tree_sha256"],
            "source": "headless_claude", "verdict": verdict,
            "findings": [{"code": "REVIEWED", "message": "Synthetic test only"}],
            "required_changes": [] if verdict == "PASS" else [
                {"id": "C1", "description": "Correct the synthetic implementation"}],
        }
        envelope = {"type": "result", "subtype": "success", "is_error": False,
                    "structured_output": payload, "session_id": "synthetic-session",
                    "usage": {"input_tokens": 50, "output_tokens": 10},
                    "total_cost_usd": None}
        if error:
            return error(command, envelope)
        return subprocess.CompletedProcess(command, 0, json.dumps(envelope), "")

    monkeypatch.setattr(module, "_run_process", invoke)
    return calls


def execute(setup_review, **kwargs):
    from company_os.headless_reviewer import run_headless_review
    company, work, repo = setup_review
    return run_headless_review(company, work.id, repository=repo,
                               test_command=[sys.executable, "-B", "-c", "assert True"],
                               executable=sys.executable,
                               idempotency_key=kwargs.pop("idempotency_key", "review-1"),
                               **kwargs)


def test_pass_is_product_bound_durable_and_idempotent(setup_review, monkeypatch):
    company, work, repo = setup_review
    calls = install_process_stub(monkeypatch, company)
    result = execute(setup_review)
    assert result["status"] == "PASS"
    assert company.work_order(work.id).status == "COMPLETED"
    assert result["source_commit"] == GitSourceSnapshot(code_root=repo).capture().source_commit
    assert result["cost_usd"] is None
    assert result["execution_id"] and result["fence_token"] == 1
    assert execute(setup_review) == result
    assert len(calls) == 2  # One deterministic test process and one Claude process.
    company.close()
    company.initialize()
    assert execute(setup_review) == result
    assert len(calls) == 2
    assert any(e["event_type"] == "HEADLESS_REVIEW_FINISHED" for e in company.events())


def test_changes_required_does_not_claim_completion(setup_review, monkeypatch):
    company, work, _ = setup_review
    install_process_stub(monkeypatch, company, verdict="CHANGES_REQUIRED")
    assert execute(setup_review)["status"] == "CHANGES_REQUIRED"
    assert company.work_order(work.id).status == "REPAIR_REQUIRED"


@pytest.mark.parametrize("kind,expected", [
    ("quota", "QUOTA_WAIT"), ("auth", "AUTH_REQUIRED"),
    ("json", "INVALID_RESPONSE"), ("nonzero", "CLI_FAILED"),
    ("mismatch", "INVALID_RESPONSE"), ("tokens", "USAGE_LIMIT_EXCEEDED"),
    ("unknown_usage", "USAGE_UNKNOWN"), ("timeout", "TIMEOUT"),
])
def test_failures_never_become_a_verdict(setup_review, monkeypatch, kind, expected):
    company, work, _ = setup_review

    def error(command, envelope):
        if kind == "timeout":
            raise subprocess.TimeoutExpired(command, 10)
        if kind in {"quota", "auth"}:
            return subprocess.CompletedProcess(command, 1, json.dumps({
                "type": "result", "is_error": True,
                "result": "rate limit exceeded" if kind == "quota" else "not logged in"}), "")
        if kind == "json":
            return subprocess.CompletedProcess(command, 0, "not json", "")
        if kind == "mismatch":
            envelope["structured_output"]["reviewed_commit"] = "0" * 40
        if kind == "tokens":
            envelope["usage"]["input_tokens"] = 999999
        if kind == "unknown_usage":
            envelope.pop("usage")
        return subprocess.CompletedProcess(command, 1 if kind == "nonzero" else 0,
                                           json.dumps(envelope), "")

    install_process_stub(monkeypatch, company, error=error)
    result = execute(setup_review)
    assert result["status"] == expected
    assert company.work_order(work.id).status == "WAITING_FOR_OPUS"
    assert not any(e["event_type"] == "REVIEW_RESULT_INGESTED" for e in company.events())


def test_product_mutation_rejects_late_result(setup_review, monkeypatch):
    company, work, repo = setup_review
    install_process_stub(monkeypatch, company, callback=lambda _: (
        repo / "app.py").write_text("ANSWER = 0\n", encoding="utf-8"))
    assert execute(setup_review)["status"] == "SOURCE_CHANGED"
    assert company.work_order(work.id).status == "WAITING_FOR_OPUS"


def test_nested_claim_cannot_start_second_reviewer(setup_review, monkeypatch):
    company, _, _ = setup_review
    def callback(_):
        with pytest.raises(ConflictError):
            execute(setup_review, idempotency_key="duplicate")
    install_process_stub(monkeypatch, company, callback=callback)
    assert execute(setup_review)["status"] == "PASS"


def test_stop_during_review_prevents_acceptance(setup_review, monkeypatch):
    company, work, _ = setup_review
    install_process_stub(monkeypatch, company, callback=lambda _: company.stop())
    assert execute(setup_review)["status"] == "STOPPED"
    assert company.work_order(work.id).status == "WAITING_FOR_OPUS"


def test_forged_headless_result_is_rejected_by_manual_ingest(setup_review):
    company, work, _ = setup_review
    review = company.prepare_review(work.id, idempotency_key="manual")
    request = json.loads(review.json_path.read_text(encoding="utf-8"))
    result = {"schema_version": 2, "review_request_id": review.id,
              "review_request_hash": review.request_hash, "source": "headless_claude",
              "reviewed_commit": request["source_commit"],
              "reviewed_tree_sha256": request["source_tree_sha256"],
              "verdict": "PASS", "findings": [], "required_changes": []}
    path = company.root / "forged.json"
    atomic_write_json(path, result)
    with pytest.raises(ValidationError, match="source"):
        company.ingest_review_result(review.id, path)


def test_tests_fail_before_spending_model_call(setup_review, monkeypatch):
    from company_os import headless_reviewer as module
    calls = []
    def invoke(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "1 failed", "")
    monkeypatch.setattr(module, "_run_process", invoke)
    assert execute(setup_review)["status"] == "TESTS_FAILED"
    assert len(calls) == 1


def test_cli_surface_is_explicit_and_bounded():
    from company_os.cli import build_parser
    args = build_parser().parse_args([
        "work", "review", "wo", "--headless", "--repository", ".",
        "--test-arg=python", "--test-arg=-m", "--test-arg=pytest",
        "--idempotency-key", "one"])
    assert args.headless and args.timeout_seconds <= 1200


def test_dashboard_includes_review_attempts(setup_review, monkeypatch):
    from company_os.dashboard import read_dashboard, _render_page
    company, _, _ = setup_review
    install_process_stub(monkeypatch, company)
    execute(setup_review)
    snapshot = read_dashboard(company.db_path)
    assert snapshot["work_orders"][0]["review_attempts"][0]["status"] == "PASS"
    assert "자동 검수 이력" in _render_page(snapshot, "test", "http://127.0.0.1:8765/")
