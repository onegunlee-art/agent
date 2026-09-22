from __future__ import annotations

import json
from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.errors import ValidationError
from company_os.fakes import FakeExecutor, FakeReviewer

from .helpers import CleanSourceSnapshotter, build_venture


def test_fake_review_requires_explicit_repair_manifest(tmp_path: Path) -> None:
    company = CompanyOS(
        root=tmp_path,
        source_snapshotter=CleanSourceSnapshotter(),
        allow_test_reviewers=True,
    )
    company.initialize()
    _, _, _, work_order = build_venture(company, "synthetic-review")
    first_run = company.execute_work_order(
        work_order.id,
        executor=FakeExecutor(),
        idempotency_key="review-first-run",
    )
    assert first_run.status == "PASS"
    review = company.prepare_review(
        work_order.id,
        idempotency_key="review-request",
    )

    result_path = FakeReviewer().write_result(company.root, review)
    ingested = company.ingest_review_result(review.id, result_path)
    assert ingested["verdict"] == "CHANGES_REQUIRED"
    assert ingested["required_changes"]

    with pytest.raises(ValidationError, match="repair manifest"):
        company.repair_once(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="review-repair-run",
        )
    assert len(company.runs_for_work_order(work_order.id)) == 1
    assert company.work_order(work_order.id).status == "REPAIR_REQUIRED"


def test_review_result_with_wrong_request_hash_is_rejected(tmp_path: Path) -> None:
    company = CompanyOS(
        root=tmp_path,
        source_snapshotter=CleanSourceSnapshotter(),
    )
    company.initialize()
    _, _, _, work_order = build_venture(company, "synthetic-bad-review")
    company.execute_work_order(
        work_order.id,
        executor=FakeExecutor(),
        idempotency_key="bad-review-run",
    )
    review = company.prepare_review(
        work_order.id,
        idempotency_key="bad-review-request",
    )
    result_path = FakeReviewer().write_result(company.root, review)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["review_request_hash"] = "0" * 64
    result_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError):
        company.ingest_review_result(review.id, result_path)
