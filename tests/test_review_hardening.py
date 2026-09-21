from __future__ import annotations

import json
from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.cli import main
from company_os.errors import ValidationError
from company_os.fakes import FakeExecutor, FakeReviewer
from company_os.utils import sha256_file

from .helpers import build_venture


def _verified_work_order(company: CompanyOS, label: str):
    _, _, _, work_order = build_venture(company, label)
    run = company.execute_work_order(
        work_order.id,
        executor=FakeExecutor(),
        idempotency_key=f"{label}-run",
    )
    assert run.status == "PASS"
    return work_order, run


def _integrity_events(company: CompanyOS) -> list[dict]:
    return [
        event
        for event in company.events()
        if event["event_type"] == "REVIEW_INPUT_TAMPER_DETECTED"
    ]


def test_review_uses_review_specific_paths_and_duplicate_prepare_returns_existing(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        work_order, _ = _verified_work_order(company, "review-unique-path")
        first = company.prepare_review(
            work_order.id,
            idempotency_key="review-unique-path-first",
        )

        second = company.prepare_review(
            work_order.id,
            idempotency_key="review-unique-path-second",
        )

        assert second.id == first.id
        assert first.json_path.parent.name == first.id
        assert first.markdown_path.parent == first.json_path.parent
        rows = company.store.query_all(
            "SELECT * FROM reviews WHERE work_order_id = ?",
            (work_order.id,),
        )
        assert len(rows) == 1

        stored_payload = json.loads(rows[0]["payload_json"])
        assert stored_payload["request_json_sha256"] == sha256_file(first.json_path)

        result_path = FakeReviewer().write_result(company.root, first)
        company.ingest_review_result(first.id, result_path)

        ingested_row = company.store.get_row("reviews", first.id)
        assert ingested_row is not None
        response_path = company._absolute(ingested_row["response_path"])
        assert response_path.parent == first.json_path.parent
        assert response_path.is_file()
        assert first.json_path.is_file()
        assert first.markdown_path.is_file()


def test_review_ingest_rejects_byte_tampered_request_and_records_event(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        work_order, _ = _verified_work_order(company, "review-request-tamper")
        review = company.prepare_review(
            work_order.id,
            idempotency_key="review-request-tamper-request",
        )
        result_path = FakeReviewer().write_result(company.root, review)

        request = json.loads(review.json_path.read_text(encoding="utf-8"))
        review.json_path.write_text(
            json.dumps(request, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        with pytest.raises(ValidationError, match="ReviewRequest integrity"):
            company.ingest_review_result(review.id, result_path)

        events = _integrity_events(company)
        assert events[-1]["payload"]["source_table"] == "review_request"
        assert company.review(review.id).status == "WAITING_FOR_OPUS"


def test_review_prepare_rejects_tampered_evidence_and_records_event(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        work_order, run = _verified_work_order(company, "review-evidence-tamper")
        verifier_evidence = next(
            item
            for item in company.evidence_for_run(run.id)
            if item.kind == "VERIFIER_OUTPUT"
        )
        verifier_evidence.path.write_text("tampered verifier output\n", encoding="utf-8")

        with pytest.raises(ValidationError, match="review input integrity"):
            company.prepare_review(
                work_order.id,
                idempotency_key="review-evidence-tamper-request",
            )

        assert company.store.query_all(
            "SELECT id FROM reviews WHERE work_order_id = ?",
            (work_order.id,),
        ) == []
        assert company.work_order(work_order.id).status == "VERIFIED"
        events = _integrity_events(company)
        assert events[-1]["payload"]["source_table"] == "evidence"


def test_review_prepare_checks_artifact_metadata_independently(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        work_order, run = _verified_work_order(company, "review-artifact-tamper")
        artifact_evidence = next(
            item
            for item in company.evidence_for_run(run.id)
            if item.kind == "RUN_ARTIFACT"
        )
        artifact_evidence.path.write_text("tampered artifact\n", encoding="utf-8")
        observed_hash = sha256_file(artifact_evidence.path)

        # Mask the Evidence row deliberately. The independently stored Artifact
        # metadata must still detect that the file changed after execution.
        with company.store.transaction() as connection:
            connection.execute(
                "UPDATE evidence SET sha256 = ? WHERE id = ?",
                (observed_hash, artifact_evidence.id),
            )

        with pytest.raises(ValidationError, match="review input integrity"):
            company.prepare_review(
                work_order.id,
                idempotency_key="review-artifact-tamper-request",
            )

        events = _integrity_events(company)
        assert events[-1]["payload"]["source_table"] == "artifacts"


def test_cli_resume_repairs_and_reverifies_changes_required_work(
    tmp_path: Path,
    capsys,
) -> None:
    with CompanyOS(tmp_path) as company:
        work_order, _ = _verified_work_order(company, "review-cli-resume")
        review = company.prepare_review(
            work_order.id,
            idempotency_key="review-cli-resume-request",
        )
        result_path = FakeReviewer().write_result(company.root, review)
        company.ingest_review_result(review.id, result_path)
        assert company.work_order(work_order.id).status == "REPAIR_REQUIRED"

    assert main(("--root", str(tmp_path), "work", "resume", work_order.id)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "PASS"

    with CompanyOS(tmp_path) as restarted:
        assert restarted.work_order(work_order.id).status == "REPAIRED_VERIFIED"
        assert len(restarted.runs_for_work_order(work_order.id)) == 2
        assert restarted.review(review.id).status == "CHANGES_APPLIED"
