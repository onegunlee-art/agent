from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from company_os.application import CompanyOS, ExistingArtifactExecutor
from company_os.errors import ValidationError
from company_os.fakes import FakeExecutor, FakeReviewer
from company_os.models import ExecutionOutput
from company_os.utils import atomic_write_text

from .helpers import CleanSourceSnapshotter, build_venture


class UnrelatedArtifactExecutor:
    name = "synthetic-unrelated-artifact-executor"

    def execute(self, work_order, workspace_path: Path) -> ExecutionOutput:
        unrelated = workspace_path / "artifacts" / "synthetic-unrelated.txt"
        atomic_write_text(unrelated, "synthetic unrelated output\n")
        return ExecutionOutput(artifact_path=unrelated)


def test_stale_expected_file_cannot_validate_an_unrelated_executor_artifact(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, venture, work_order = build_venture(company, "stale-artifact-hardening")
        stale_expected = venture.workspace_path / work_order.artifact_relative_path
        atomic_write_text(stale_expected, work_order.expected_content)

        run = company.execute_work_order(
            work_order.id,
            executor=UnrelatedArtifactExecutor(),
            idempotency_key="stale-artifact-hardening-run",
        )

        unrelated = venture.workspace_path / "artifacts" / "synthetic-unrelated.txt"
        evidence = company.evidence_for_run(run.id)
        unrelated_evidence = [item for item in evidence if item.path == unrelated]

        assert run.status != "PASS"
        assert unrelated_evidence
        assert all(item.trusted is False for item in unrelated_evidence)
        assert not any(item.path == unrelated and item.trusted for item in evidence)


def test_deleted_verifier_invalidates_run_and_records_untrusted_evidence(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "missing-verifier-hardening")
        work_order.verifier_path.unlink()

        run = company.execute_work_order(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="missing-verifier-hardening-run",
        )

        evidence = company.evidence_for_run(run.id)
        event_types = {event["event_type"] for event in company.events()}

        assert run.status == "INVALIDATED"
        assert company.work_order(work_order.id).status == "INVALIDATED"
        assert "VERIFIER_TAMPER_DETECTED" in event_types
        assert evidence
        assert any(item.kind == "TAMPERED_VERIFIER" for item in evidence)
        assert all(item.trusted is False for item in evidence)


def test_failed_content_verification_can_retry_to_a_second_passing_run(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, venture, work_order = build_venture(company, "failed-retry-hardening")
        artifact = venture.workspace_path / work_order.artifact_relative_path
        atomic_write_text(artifact, "synthetic incorrect content\n")

        failed = company.verify_existing_artifact(
            work_order.id,
            idempotency_key="failed-retry-hardening-attempt-1",
        )

        failed_events = [
            event
            for event in company.events()
            if event["event_type"] == "WORK_ORDER_VERIFIED"
            and event["payload"]["run_id"] == failed.id
        ]
        assert failed.status == "FAIL"
        assert failed_events
        assert failed_events[0]["payload"]["status"] == "FAIL"

        atomic_write_text(artifact, work_order.expected_content)
        passed = company.resume_work_order(
            work_order.id,
            executor=ExistingArtifactExecutor(),
        )
        runs = company.runs_for_work_order(work_order.id)

        assert passed.status == "PASS"
        assert passed.id != failed.id
        assert [(run.attempt, run.status) for run in runs] == [(1, "FAIL"), (2, "PASS")]
        assert company.work_order(work_order.id).status == "VERIFIED"


def test_concurrent_execute_allows_exactly_one_passing_transition(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as seed:
        _, _, _, work_order = build_venture(seed, "concurrent-execute-hardening")

    first_company = CompanyOS(tmp_path).initialize()
    second_company = CompanyOS(tmp_path).initialize()
    barrier = Barrier(2)
    try:
        for company in (first_company, second_company):
            original_run_idempotent = company.store.run_idempotent

            def synchronized_run_idempotent(
                key,
                command,
                payload,
                operation,
                *,
                _original=original_run_idempotent,
            ):
                barrier.wait(timeout=10)
                return _original(key, command, payload, operation)

            company.store.run_idempotent = synchronized_run_idempotent

        def execute(company: CompanyOS, key: str):
            try:
                return company.execute_work_order(
                    work_order.id,
                    executor=FakeExecutor(),
                    idempotency_key=key,
                )
            except ValidationError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(execute, first_company, "concurrent-execute-a"),
                executor.submit(execute, second_company, "concurrent-execute-b"),
            )
            outcomes = [future.result(timeout=20) for future in futures]

        passing = [outcome for outcome in outcomes if getattr(outcome, "status", None) == "PASS"]
        rejected = [outcome for outcome in outcomes if isinstance(outcome, ValidationError)]
        stored_runs = first_company.runs_for_work_order(work_order.id)

        assert len(passing) == 1
        assert len(rejected) == 1
        assert len(stored_runs) == 1
        assert stored_runs[0].status == "PASS"
    finally:
        first_company.close()
        second_company.close()


def test_repair_without_manifest_cannot_create_a_second_run(
    tmp_path: Path,
) -> None:
    with CompanyOS(
        tmp_path,
        source_snapshotter=CleanSourceSnapshotter(),
        allow_test_reviewers=True,
    ) as company:
        _, _, _, work_order = build_venture(company, "repair-replay-hardening")
        company.execute_work_order(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="repair-replay-initial-run",
        )
        review = company.prepare_review(
            work_order.id,
            idempotency_key="repair-replay-review",
        )
        review_result = FakeReviewer().write_result(company.root, review)
        company.ingest_review_result(review.id, review_result)

        with pytest.raises(ValidationError, match="repair manifest"):
            company.repair_once(
                work_order.id,
                executor=FakeExecutor(),
                idempotency_key="repair-replay-same-key",
            )

        repair_events = [
            event
            for event in company.events()
            if event["event_type"] == "REPAIR_REVERIFIED"
        ]
        runs = company.runs_for_work_order(work_order.id)

        assert company.work_order(work_order.id).status == "REPAIR_REQUIRED"
        assert len(runs) == 1
        assert len(repair_events) == 0
