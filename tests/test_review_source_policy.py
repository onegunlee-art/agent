from __future__ import annotations

from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.cli import main
from company_os.errors import ValidationError
from company_os.fakes import FakeExecutor, FakeReviewer
from company_os.handoffs import validate_review_result

from .helpers import CleanSourceSnapshotter, build_venture


REQUEST_ID = "review_test_source_policy"
REQUEST_HASH = "a" * 64


def _review_result(source: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "review_request_id": REQUEST_ID,
        "review_request_hash": REQUEST_HASH,
        "source": source,
        "verdict": "PASS",
        "findings": [],
        "required_changes": [],
    }


def test_review_result_rejects_fake_reviewer_by_default() -> None:
    with pytest.raises(ValidationError, match="source must be user_supplied"):
        validate_review_result(
            _review_result("fake_reviewer"),
            request_id=REQUEST_ID,
            request_hash=REQUEST_HASH,
        )


def test_review_result_allows_fake_reviewer_only_when_explicitly_enabled() -> None:
    validate_review_result(
        _review_result("fake_reviewer"),
        request_id=REQUEST_ID,
        request_hash=REQUEST_HASH,
        allow_fake_reviewer=True,
    )


def test_cli_rejects_fake_reviewer_without_any_state_transition(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshotter = CleanSourceSnapshotter()
    with CompanyOS(tmp_path, source_snapshotter=snapshotter) as company:
        _, _, _, work_order = build_venture(company, "cli-fake-reviewer-policy")
        company.execute_work_order(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="cli-fake-reviewer-run",
        )
        review = company.prepare_review(
            work_order.id,
            idempotency_key="cli-fake-reviewer-request",
        )
        result_path = FakeReviewer().write_result(company.root, review)
        event_count = len(company.events())
        decision_count = int(company.store.scalar("SELECT COUNT(*) FROM decisions"))

    monkeypatch.setattr(
        "company_os.cli.CompanyOS",
        lambda root, db_path=None: CompanyOS(
            root, db_path=db_path, source_snapshotter=snapshotter
        ),
    )
    exit_code = main(
        (
            "--root",
            str(tmp_path),
            "review",
            "ingest",
            review.id,
            "--file",
            str(result_path),
        )
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "source must be user_supplied" in captured.err

    with CompanyOS(tmp_path, source_snapshotter=snapshotter) as restarted:
        row = restarted.store.get_row("reviews", review.id)
        assert row is not None
        assert row["status"] == "WAITING_FOR_OPUS"
        assert row["response_path"] is None
        assert restarted.work_order(work_order.id).status == "WAITING_FOR_OPUS"
        assert len(restarted.events()) == event_count
        assert restarted.store.scalar("SELECT COUNT(*) FROM decisions") == decision_count
        assert (
            restarted.store.get_row("idempotency", f"review-ingest:{review.id}")
            is None
        )
