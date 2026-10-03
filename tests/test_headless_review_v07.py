from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import os
from hashlib import sha256

import pytest

from company_os.application import CompanyOS
from company_os.errors import ConflictError, ValidationError
from company_os.fakes import FakeExecutor
from company_os.source_snapshot import GitSourceSnapshot, SourceSnapshot, SourceSnapshotError
from company_os.utils import atomic_write_json
from .helpers import CleanSourceSnapshotter, build_venture


@pytest.fixture
def setup_review(tmp_path, monkeypatch, request):
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
    if request.node.name != "test_real_git_product_binding" and not request.node.get_closest_marker("integration"):
        # Orchestration tests isolate the already-covered Git snapshot adapter.
        # One real-adapter acceptance test below still exercises the full path.
        from company_os import application, headless_reviewer
        initial = (repo / "app.py").read_bytes()
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                capture_output=True, text=True, timeout=20, check=True).stdout.strip()
        class ProductSnapshot:
            def __init__(self, *, code_root):
                assert Path(code_root).resolve() == repo.resolve()
            def capture(self):
                if (repo / "app.py").read_bytes() != initial:
                    raise SourceSnapshotError("dirty product")
                return SourceSnapshot(commit, "d" * 40, sha256(initial).hexdigest(), False)
        monkeypatch.setattr(application, "GitSourceSnapshot", ProductSnapshot)
        monkeypatch.setattr(headless_reviewer, "GitSourceSnapshot", ProductSnapshot)
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
    request = json.loads(company.review(result["review_id"]).json_path.read_text(encoding="utf-8"))
    assert result["source_commit"] == request["source_commit"] != "a" * 40
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


@pytest.mark.slow
def test_real_git_product_binding(setup_review, monkeypatch):
    company, _, repo = setup_review
    install_process_stub(monkeypatch, company)
    result = execute(setup_review)
    assert result["status"] == "PASS"
    assert result["source_tree_sha256"] == GitSourceSnapshot(code_root=repo).capture().source_tree_sha256


def test_expired_attempt_is_reclaimed_and_old_result_cannot_win(setup_review, monkeypatch):
    from company_os import headless_reviewer as module
    company, _, _ = setup_review
    nested = []
    def callback(request):
        if nested:
            return
        nested.append(True)
        key = module._state_key(request["review_request_id"])
        state = company.store.get_global_state(key)
        company.store.set_global_state(key, {**state, "expires_at": 0})
        nested.append(execute(setup_review, idempotency_key="after-restart"))
    install_process_stub(monkeypatch, company, callback=callback)
    old = execute(setup_review)
    assert old["status"] == "STALE_RESULT_REJECTED"
    assert nested[1]["status"] == "PASS"
    assert nested[1]["fence_token"] == 2
    assert sum(e["event_type"] == "REVIEW_RESULT_INGESTED" for e in company.events()) == 1
    assert any(e["event_type"] == "HEADLESS_REVIEW_EXPIRED" for e in company.events())


def test_subscription_environment_excludes_api_keys(monkeypatch, tmp_path):
    from company_os.headless_reviewer import _environment
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-not-a-key")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-not-a-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-subscription-token")
    env = _environment(tmp_path)
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-subscription-token"


def test_manual_refresh_cannot_rebind_product_review_to_kernel(setup_review, monkeypatch):
    company, work, repo = setup_review
    review = company.prepare_review(work.id, idempotency_key="prepare", source_repository=repo)
    again = company.prepare_review(work.id, idempotency_key="manual-refresh")
    assert again.id == review.id
    assert json.loads(again.json_path.read_text(encoding="utf-8"))["source_repository"] == str(repo.resolve())


@pytest.mark.integration
def test_live_claude_review_only_when_explicitly_enabled(setup_review):
    from company_os.headless_reviewer import run_headless_review
    binary = os.environ.get("COMPANY_LIVE_CLAUDE")
    if not binary:
        pytest.skip("Set COMPANY_LIVE_CLAUDE to explicitly authorize a subscription call")
    company, work, repo = setup_review
    receipt = run_headless_review(
        company, work.id, repository=repo,
        test_command=[sys.executable, "-B", "-c", "assert True"],
        executable=binary, idempotency_key="explicit-live-integration")
    assert receipt["status"] in {"PASS", "CHANGES_REQUIRED"}


def test_claim_rechecks_review_state_before_starting_process(setup_review, monkeypatch):
    company, work, _ = setup_review
    original = company._review_resolution_integrity_issues
    def concurrent_completion(row, **kwargs):
        result = original(row, **kwargs)
        company.store.connection.execute("UPDATE reviews SET status = 'COMPLETED' WHERE id = ?", (row["id"],))
        return result
    monkeypatch.setattr(company, "_review_resolution_integrity_issues", concurrent_completion)
    calls = install_process_stub(monkeypatch, company)
    with pytest.raises(ConflictError, match="waiting"):
        execute(setup_review)
    assert not calls


def test_error_classifier_does_not_read_status_codes_from_source_hashes():
    from company_os.headless_reviewer import _decode
    envelope = {"type": "result", "is_error": False, "subtype": "success",
                "structured_output": {"reviewed_commit": "abc401def429abc"}}
    process = subprocess.CompletedProcess(["claude"], 1, json.dumps(envelope), "")
    assert _decode(process, {}, 100)[0] == "CLI_FAILED"
