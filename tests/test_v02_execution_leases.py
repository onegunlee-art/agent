from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest

from company_os.application import CompanyOS, ExistingArtifactExecutor
from company_os.errors import CompanyStoppedError, StaleExecutionError, ValidationError
from company_os.fakes import FakeExecutor
from company_os.models import ExecutionOutput
from company_os.utils import atomic_write_text

from .helpers import CleanSourceSnapshotter, build_venture


class ProbeExecutor:
    name = "synthetic-probe-executor"

    def __init__(self) -> None:
        self.called = False

    def execute(self, work_order, workspace_path: Path) -> ExecutionOutput:
        self.called = True
        artifact = workspace_path / work_order.artifact_relative_path
        atomic_write_text(artifact, work_order.expected_content)
        return ExecutionOutput(artifact_path=artifact)


class BlockingExecutor:
    name = "synthetic-v02-blocking-executor"

    def __init__(self, entered: Event, release: Event) -> None:
        self.entered = entered
        self.release = release

    def execute(self, work_order, workspace_path: Path) -> ExecutionOutput:
        assert not getattr(work_order, "_connection_in_transaction", False)
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise RuntimeError("synthetic blocking executor timed out")
        artifact = workspace_path / work_order.artifact_relative_path
        atomic_write_text(artifact, work_order.expected_content)
        return ExecutionOutput(artifact_path=artifact)


class ReportedOutcomeExecutor:
    name = "synthetic-reported-outcome"

    def __init__(self, *, ok: bool, label: str) -> None:
        self.ok = ok
        self.label = label

    def execute(self, work_order, workspace_path: Path) -> ExecutionOutput:
        artifact = workspace_path / work_order.artifact_relative_path
        if self.ok:
            atomic_write_text(artifact, work_order.expected_content)
        return ExecutionOutput(
            artifact_path=artifact,
            ok=self.ok,
            label=self.label,
            cost_usd=None,
            cost_unknown=True,
            error=None if self.ok else "workspace is read-only",
        )


def _event_types(company: CompanyOS) -> list[str]:
    return [event["event_type"] for event in company.events()]


def test_prepare_review_rehashes_verifier_and_records_tamper(tmp_path: Path) -> None:
    with CompanyOS(
        tmp_path,
        source_snapshotter=CleanSourceSnapshotter(),
    ) as company:
        _, _, _, work_order = build_venture(company, "review-verifier-rehash")
        company.execute_work_order(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="review-verifier-rehash-run",
        )
        work_order.verifier_path.write_text('{"tampered": true}\n', encoding="utf-8")

        with pytest.raises(ValidationError, match="verifier"):
            company.prepare_review(
                work_order.id,
                idempotency_key="review-verifier-rehash-review",
            )

        assert "VERIFIER_TAMPERED" in _event_types(company)
        assert not company.store.query_all(
            "SELECT id FROM reviews WHERE work_order_id = ?", (work_order.id,)
        )


def test_stop_committed_between_preflight_and_claim_rejects_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with CompanyOS(tmp_path) as runner, CompanyOS(tmp_path) as stopper:
        _, _, _, work_order = build_venture(runner, "stop-claim-race")
        executor = ProbeExecutor()
        original = runner.is_stopped
        calls = 0

        def racing_preflight() -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                stopper.stop()
                return False
            return original()

        monkeypatch.setattr(runner, "is_stopped", racing_preflight)
        with pytest.raises(CompanyStoppedError):
            runner.execute_work_order(
                work_order.id,
                executor=executor,
                idempotency_key="stop-claim-race-run",
            )

        assert executor.called is False
        assert runner.work_order(work_order.id).status == "READY"
        assert "WORK_ORDER_CLAIM_REJECTED" in _event_types(runner)


def test_claim_committed_before_stop_can_finish(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as seed:
        _, _, _, work_order = build_venture(seed, "claim-before-stop")

    runner = CompanyOS(tmp_path).initialize()
    stopper = CompanyOS(tmp_path).initialize()
    entered, release = Event(), Event()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(
                runner.execute_work_order,
                work_order.id,
                executor=BlockingExecutor(entered, release),
                idempotency_key="claim-before-stop-run",
            )
            assert entered.wait(timeout=5)
            stopper.stop()
            release.set()
            run = future.result(timeout=10)

        assert run.status == "PASS"
        assert runner.work_order(work_order.id).status == "VERIFIED"
    finally:
        release.set()
        runner.close()
        stopper.close()


def test_missing_artifact_records_run_event_and_evidence(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "missing-artifact-run")

        with pytest.raises(ValidationError, match="artifact is missing"):
            company.execute_work_order(
                work_order.id,
                executor=ExistingArtifactExecutor(),
                idempotency_key="missing-artifact-run-attempt",
            )

        runs = company.runs_for_work_order(work_order.id)
        assert len(runs) == 1
        assert runs[0].status == "MISSING_ARTIFACT"
        assert any(
            item.kind == "EXECUTION_FAILURE"
            for item in company.evidence_for_run(runs[0].id)
        )
        assert "WORK_ORDER_EXECUTION_FAILED" in _event_types(company)
        assert company.work_order(work_order.id).status == "READY"


def test_different_work_orders_can_execute_concurrently(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as seed:
        _, _, _, first = build_venture(seed, "parallel-work-one")
        _, _, _, second = build_venture(seed, "parallel-work-two")

    one = CompanyOS(tmp_path).initialize()
    two = CompanyOS(tmp_path).initialize()
    entered_one, entered_two, release = Event(), Event(), Event()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(
                one.execute_work_order,
                first.id,
                executor=BlockingExecutor(entered_one, release),
                idempotency_key="parallel-work-one-run",
            )
            second_future = pool.submit(
                two.execute_work_order,
                second.id,
                executor=BlockingExecutor(entered_two, release),
                idempotency_key="parallel-work-two-run",
            )
            assert entered_one.wait(timeout=5)
            assert entered_two.wait(timeout=5)
            statuses = {
                one.work_order(first.id).status,
                two.work_order(second.id).status,
            }
            assert statuses == {"EXECUTING"}
            release.set()
            assert first_future.result(timeout=10).status == "PASS"
            assert second_future.result(timeout=10).status == "PASS"
    finally:
        release.set()
        one.close()
        two.close()


def test_expired_workspace_lease_reclaims_and_rejects_late_result(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as seed:
        _, _, _, work_order = build_venture(seed, "expired-workspace-lease")
        with seed.store.transaction() as connection:
            connection.execute(
                "UPDATE work_orders SET time_limit_seconds = 1 WHERE id = ?",
                (work_order.id,),
            )

    runner = CompanyOS(tmp_path).initialize()
    reclaimer = CompanyOS(tmp_path).initialize()
    entered, release = Event(), Event()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(
                runner.execute_work_order,
                work_order.id,
                executor=BlockingExecutor(entered, release),
                idempotency_key="expired-workspace-lease-run",
            )
            assert entered.wait(timeout=5)
            reclaimed = reclaimer.reclaim_expired(
                now=datetime.now(timezone.utc) + timedelta(minutes=5)
            )
            assert [item["work_order_id"] for item in reclaimed] == [work_order.id]
            assert reclaimer.work_order(work_order.id).status == "READY"
            release.set()
            with pytest.raises(StaleExecutionError):
                future.result(timeout=10)

        runs = reclaimer.runs_for_work_order(work_order.id)
        assert len(runs) == 1 and runs[0].status == "EXPIRED"
        assert "LEASE_RECLAIMED" in _event_types(reclaimer)
        assert "STALE_RESULT_REJECTED" in _event_types(reclaimer)
    finally:
        release.set()
        runner.close()
        reclaimer.close()


def test_expired_external_lease_requires_manual_recovery(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "expired-external-lease")
        with company.store.transaction() as connection:
            connection.execute(
                """
                UPDATE work_orders
                SET status = 'EXECUTING', execution_id = 'execution_external',
                    fence_token = 1, lease_expires_at = ?, side_effect_class = 'EXTERNAL'
                WHERE id = ?
                """,
                ("2000-01-01T00:00:00+00:00", work_order.id),
            )

        company.reclaim_expired(now=datetime.now(timezone.utc))

        assert company.work_order(work_order.id).status == "MANUAL_RECOVERY_REQUIRED"


def test_execution_keeps_permission_failure_separate_from_unknown_cost(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "permission-vs-cost")
        run = company.execute_work_order(
            work_order.id,
            executor=ReportedOutcomeExecutor(
                ok=False,
                label="WRITE_PERMISSION_DENIED",
            ),
            idempotency_key="permission-vs-cost-run",
        )
        assert run.outcome == "WRITE_PERMISSION_DENIED"
        assert run.payload["cost_status"] == "UNAVAILABLE"
        assert company.work_order(work_order.id).status == "READY"


def test_successful_execution_with_unknown_cost_is_cost_unavailable(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "unknown-cost")
        run = company.execute_work_order(
            work_order.id,
            executor=ReportedOutcomeExecutor(ok=True, label="DONE"),
            idempotency_key="unknown-cost-run",
        )
        assert run.outcome == "COST_UNAVAILABLE"
        assert run.payload["cost_status"] == "UNAVAILABLE"
        assert company.work_order(work_order.id).status == "FAILED"


def test_work_order_has_subscription_usage_limits(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "subscription-usage-limits")
        assert work_order.model_call_limit == 1
        assert work_order.token_limit == 250_000
