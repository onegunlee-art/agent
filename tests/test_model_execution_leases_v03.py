from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import time

import pytest

from company_os.application import CompanyOS
from company_os.cli import _dispatch, build_parser
from company_os.errors import CompanyStoppedError, StaleExecutionError, ValidationError
from company_os.model_executor import ExecutorOutcome, ExecutorRequest

from .helpers import build_venture


def _request(
    work_order_id: str,
    workspace: Path,
    *,
    time_limit_seconds: int = 1200,
) -> ExecutorRequest:
    workspace.mkdir(exist_ok=True)
    return ExecutorRequest(
        work_order_id=work_order_id,
        workspace=workspace,
        instructions="Change one synthetic fixture and run its acceptance test.",
        time_limit_seconds=time_limit_seconds,
        cost_limit_usd=2.0,
        model_call_limit=1,
        token_limit=250_000,
        test_command=("python", "acceptance_test.py"),
    )


def _success() -> ExecutorOutcome:
    return ExecutorOutcome(
        ok=True,
        label="DONE",
        cost_usd=None,
        cost_unknown=True,
        duration_seconds=0.25,
        changed_files=["synthetic.py"],
        usage={"input_tokens": 80, "output_tokens": 20},
        usage_status="WITHIN_LIMIT",
        usage_total_tokens=100,
        workspace_branch="wo/synthetic",
        workspace_head="a" * 40,
        sparse_checkout_patterns=["src", "tests"],
        workspace_diff="diff --git a/synthetic.py b/synthetic.py\n",
        changed_file_sha256={"synthetic.py": "b" * 64},
    )


class ProbeModelExecutor:
    def __init__(self, company: CompanyOS, work_order_id: str) -> None:
        self.company = company
        self.work_order_id = work_order_id
        self.observed_execution_id: str | None = None
        self.observed_fence_token: int | None = None

    def run(self, request: ExecutorRequest) -> ExecutorOutcome:
        claimed = self.company.work_order(self.work_order_id)
        assert claimed.status == "EXECUTING"
        assert claimed.execution_id is not None
        assert claimed.fence_token > 0
        self.observed_execution_id = claimed.execution_id
        self.observed_fence_token = claimed.fence_token
        # A separate short write must succeed while the model process is running.
        with self.company.store.transaction() as connection:
            self.company.store.append_event(
                "MODEL_EXECUTOR_OUTSIDE_WRITE_TRANSACTION",
                aggregate_type="WorkOrder",
                aggregate_id=self.work_order_id,
                payload={"observed": True},
                connection=connection,
            )
        return _success()


def test_production_model_run_owns_lease_and_finalizes_auditable_run(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "v03-model-lease-success")
        executor = ProbeModelExecutor(company, work_order.id)

        run, outcome = company.execute_model_work_order(
            work_order.id,
            executor=executor,
            request=_request(work_order.id, tmp_path / "model-worktree"),
            idempotency_key="v03-model-lease-success",
        )

        assert outcome.label == "DONE"
        assert run.execution_id == executor.observed_execution_id
        assert run.fence_token == executor.observed_fence_token
        assert run.payload["diagnostic_only"] is False
        assert run.payload["production_execution"] is True
        assert run.payload["usage_status"] == "WITHIN_LIMIT"
        released = company.work_order(work_order.id)
        assert released.status == "READY"
        assert released.execution_id is None
        assert released.lease_expires_at is None
        assert released.fence_token == run.fence_token + 1

        evidence = company.evidence_for_run(run.id)
        model_evidence = [item for item in evidence if item.kind == "MODEL_EXECUTION"]
        assert len(model_evidence) == 1
        assert model_evidence[0].trusted is True
        event_types = {item["event_type"] for item in company.events()}
        assert "WORK_ORDER_EXECUTION_STARTED" in event_types
        assert "MODEL_EXECUTION_FINALIZED" in event_types


class ReclaimBeforeReturnExecutor:
    def __init__(self, company: CompanyOS) -> None:
        self.company = company

    def run(self, request: ExecutorRequest) -> ExecutorOutcome:
        reclaimed = self.company.reclaim_expired(
            now=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        assert len(reclaimed) == 1
        return _success()


class ReturnAfterDeadlineExecutor:
    def run(self, request: ExecutorRequest) -> ExecutorOutcome:
        time.sleep(1.1)
        return _success()


def test_reclaimed_model_lease_rejects_late_result_without_model_evidence(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "v03-model-lease-stale")

        with pytest.raises(StaleExecutionError, match="Late model execution result"):
            company.execute_model_work_order(
                work_order.id,
                executor=ReclaimBeforeReturnExecutor(company),
                request=_request(work_order.id, tmp_path / "stale-worktree"),
                idempotency_key="v03-model-lease-stale",
            )

        runs = company.runs_for_work_order(work_order.id)
        assert len(runs) == 1
        assert runs[0].outcome == "EXPIRED"
        assert runs[0].execution_id is not None
        assert not company.evidence_for_run(runs[0].id)
        assert "STALE_RESULT_REJECTED" in {
            item["event_type"] for item in company.events()
        }


def test_expired_model_result_is_failure_evidence_not_production_evidence(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, initial = build_venture(company, "v03-model-lease-expired")
        with company.store.transaction() as connection:
            connection.execute(
                "UPDATE work_orders SET time_limit_seconds = 1 WHERE id = ?",
                (initial.id,),
            )
        work_order = company.work_order(initial.id)

        run, _ = company.execute_model_work_order(
            work_order.id,
            executor=ReturnAfterDeadlineExecutor(),
            request=_request(
                work_order.id,
                tmp_path / "expired-worktree",
                time_limit_seconds=1,
            ),
            idempotency_key="v03-model-lease-expired",
        )

        assert run.outcome == "EXPIRED"
        evidence_kinds = {item.kind for item in company.evidence_for_run(run.id)}
        assert "MODEL_EXECUTION" not in evidence_kinds
        assert evidence_kinds == {"MODEL_EXECUTION_FAILURE"}
        assert company.work_order(work_order.id).status == "READY"
        with pytest.raises(ValidationError, match="successful Run"):
            company.run_official_evaluation(
                work_order.id,
                run.id,
                cases_path=tmp_path / "missing-cases.json",
                data_path=tmp_path / "missing-data.json",
                approval_id="approval_not_reached",
                idempotency_key="expired-run-must-not-evaluate",
            )


class RaisingModelExecutor:
    def run(self, request: ExecutorRequest) -> ExecutorOutcome:
        raise RuntimeError("synthetic executor crash")


class StaticModelExecutor:
    def run(self, request: ExecutorRequest) -> ExecutorOutcome:
        return _success()


class ForbiddenModelExecutor:
    called = False

    def run(self, request: ExecutorRequest) -> ExecutorOutcome:
        self.called = True
        raise AssertionError("stopped company must not invoke the model executor")


def test_model_executor_crash_is_recorded_and_new_claim_can_retry(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "v03-model-lease-crash")

        with pytest.raises(RuntimeError, match="synthetic executor crash"):
            company.execute_model_work_order(
                work_order.id,
                executor=RaisingModelExecutor(),
                request=_request(work_order.id, tmp_path / "crash-worktree"),
                idempotency_key="v03-model-lease-crash",
            )

        failed = company.runs_for_work_order(work_order.id)
        assert len(failed) == 1
        assert failed[0].outcome == "ERROR"
        assert failed[0].execution_id is not None
        assert failed[0].fence_token is not None
        assert failed[0].payload["diagnostic_only"] is False
        assert company.work_order(work_order.id).status == "READY"

        retry, _ = company.execute_model_work_order(
            work_order.id,
            executor=StaticModelExecutor(),
            request=_request(work_order.id, tmp_path / "retry-worktree"),
            idempotency_key="v03-model-lease-retry",
        )
        assert retry.outcome == "DONE"
        assert retry.execution_id != failed[0].execution_id
        assert retry.fence_token > failed[0].fence_token


def test_stopped_company_rejects_model_claim_before_idempotency_or_executor(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "v03-model-lease-stopped")
        company.stop()
        executor = ForbiddenModelExecutor()

        with pytest.raises(CompanyStoppedError):
            company.execute_model_work_order(
                work_order.id,
                executor=executor,
                request=_request(work_order.id, tmp_path / "stopped-worktree"),
                idempotency_key="v03-model-lease-stopped",
            )

        assert executor.called is False
        assert company.store.get_row("idempotency", "v03-model-lease-stopped") is None
        rejected = [
            event
            for event in company.events()
            if event["event_type"] == "WORK_ORDER_CLAIM_REJECTED"
        ]
        assert rejected[-1]["payload"]["execution_kind"] == "MODEL"


def test_cli_model_run_uses_production_lease_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=repository,
        check=True,
        timeout=20,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repository,
        check=True,
        timeout=20,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=repository,
        check=True,
        timeout=20,
    )
    (repository / "synthetic.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=repository, check=True, timeout=20
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "fixture"],
        cwd=repository,
        check=True,
        timeout=20,
    )
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("Update the synthetic fixture.\n", encoding="utf-8")

    with CompanyOS(tmp_path / "company") as company:
        _, _, _, work_order = build_venture(company, "v03-model-lease-cli")
        monkeypatch.setattr(
            "company_os.cli.CliCodingExecutor",
            lambda: StaticModelExecutor(),
        )
        args = build_parser().parse_args(
            [
                "work",
                "model-run",
                work_order.id,
                "--repository",
                str(repository),
                "--worktree",
                str(tmp_path / "worktree"),
                "--branch",
                "wo/v03-cli-lease",
                "--instructions-file",
                str(instructions),
                "--test-arg",
                "python",
                "--test-arg",
                "acceptance_test.py",
                "--idempotency-key",
                "v03-model-lease-cli",
            ]
        )

        result = _dispatch(company, args)

        run = result["run"]
        assert run.execution_id is not None
        assert run.fence_token is not None
        assert run.payload["diagnostic_only"] is False
        assert result["executor_outcome"] == "DONE"
        assert result["reproducibility_evidence_id"]


def test_cli_model_run_rejects_result_returned_after_lease_reclaim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=repository,
        check=True,
        timeout=20,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repository,
        check=True,
        timeout=20,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=repository,
        check=True,
        timeout=20,
    )
    (repository / "synthetic.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=repository, check=True, timeout=20
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "fixture"],
        cwd=repository,
        check=True,
        timeout=20,
    )
    instructions = tmp_path / "instructions.txt"
    instructions.write_text("Update the synthetic fixture.\n", encoding="utf-8")

    with CompanyOS(tmp_path / "company") as company:
        _, _, _, work_order = build_venture(company, "v03-model-lease-cli-stale")
        monkeypatch.setattr(
            "company_os.cli.CliCodingExecutor",
            lambda: ReclaimBeforeReturnExecutor(company),
        )
        args = build_parser().parse_args(
            [
                "work",
                "model-run",
                work_order.id,
                "--repository",
                str(repository),
                "--worktree",
                str(tmp_path / "worktree"),
                "--branch",
                "wo/v03-cli-stale",
                "--instructions-file",
                str(instructions),
                "--test-arg",
                "python",
                "--test-arg",
                "acceptance_test.py",
                "--idempotency-key",
                "v03-model-lease-cli-stale",
            ]
        )

        with pytest.raises(StaleExecutionError, match="Late model execution result"):
            _dispatch(company, args)

        runs = company.runs_for_work_order(work_order.id)
        assert len(runs) == 1
        assert runs[0].outcome == "EXPIRED"
        assert runs[0].execution_id is not None
        assert not company.evidence_for_run(runs[0].id)
