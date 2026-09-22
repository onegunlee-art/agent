from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.errors import ValidationError
from company_os.fakes import FakeExecutor
from company_os.source_snapshot import SourceSnapshot
from company_os.utils import read_json

from .helpers import build_venture


@dataclass
class MutableSourceSnapshot:
    snapshot: SourceSnapshot

    def capture(self) -> SourceSnapshot:
        return self.snapshot


def _snapshot(commit: str = "a" * 40, tree: str = "b" * 64) -> SourceSnapshot:
    return SourceSnapshot(
        source_commit=commit,
        source_tree_oid="c" * 40,
        source_tree_sha256=tree,
        dirty=False,
    )


def _review_result(review, *, verdict: str, changes: list[dict] | None = None) -> dict:
    request = read_json(review.json_path)
    return {
        "schema_version": 2,
        "review_request_id": review.id,
        "review_request_hash": review.request_hash,
        "reviewed_commit": request["source_commit"],
        "reviewed_tree_sha256": request["source_tree_sha256"],
        "source": "user_supplied",
        "verdict": verdict,
        "findings": [],
        "required_changes": changes or [],
    }


def _write_result(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _verified_review(company: CompanyOS, label: str):
    _, _, _, work_order = build_venture(company, label)
    run = company.execute_work_order(
        work_order.id,
        executor=FakeExecutor(),
        idempotency_key=f"{label}-run",
    )
    review = company.prepare_review(
        work_order.id,
        idempotency_key=f"{label}-review",
    )
    return work_order, run, review


def test_review_request_v2_is_self_contained_and_bound_to_source(
    tmp_path: Path,
) -> None:
    source = MutableSourceSnapshot(_snapshot())
    with CompanyOS(tmp_path, source_snapshotter=source) as company:
        work_order, run, review = _verified_review(company, "review-v2-content")
        request = read_json(review.json_path)

        assert request["schema_version"] == 2
        assert request["source_commit"] == "a" * 40
        assert request["source_tree_sha256"] == "b" * 64
        assert request["work_order"]["objective"]
        assert request["work_order"]["acceptance"]["pass_condition"]
        assert request["verifier"]["spec"]
        assert request["verifier_output"]["content"]["run_id"] == run.id
        assert request["artifacts"][0]["content"]
        assert request["artifacts"][0]["sha256"]
        assert request["parent_review_id"] is None
        assert "reviewed_commit" in request["response_schema"]["required"]
        assert "reviewed_tree_sha256" in request["response_schema"]["required"]

        bad = _review_result(review, verdict="PASS")
        bad["reviewed_tree_sha256"] = "f" * 64
        result_path = _write_result(tmp_path / "bad-result.json", bad)
        with pytest.raises(ValidationError, match="tree"):
            company.ingest_review_result(review.id, result_path)
        assert company.work_order(work_order.id).status == "WAITING_FOR_OPUS"


def test_repair_requires_change_and_only_rereview_pass_completes(
    tmp_path: Path,
) -> None:
    source = MutableSourceSnapshot(_snapshot())
    with CompanyOS(
        tmp_path,
        source_snapshotter=source,
    ) as company:
        work_order, initial_run, review = _verified_review(
            company, "review-rereview"
        )
        changes = [
            {"id": "CHANGE-1", "description": "Bind the result to changed source."},
            {"id": "CHANGE-2", "description": "Preserve remediation evidence."},
        ]
        result_path = _write_result(
            tmp_path / "changes-required.json",
            _review_result(review, verdict="CHANGES_REQUIRED", changes=changes),
        )
        company.ingest_review_result(review.id, result_path)

        evidence_id = next(
            evidence.id
            for evidence in company.evidence_for_run(initial_run.id)
            if evidence.kind == "VERIFIER_OUTPUT"
        )

        def manifest() -> dict:
            return {
                "schema_version": 1,
                "review_id": review.id,
                "source_commit": source.snapshot.source_commit,
                "source_tree_sha256": source.snapshot.source_tree_sha256,
                "changes": [
                    {
                        "id": item["id"],
                        "commit": source.snapshot.source_commit,
                        "evidence_ids": [evidence_id],
                    }
                    for item in changes
                ],
            }

        with pytest.raises(ValidationError, match="no substantive change"):
            company.repair_once(
                work_order.id,
                executor=FakeExecutor(),
                idempotency_key="unchanged-repair",
                repair_manifest=manifest(),
            )
        assert company.work_order(work_order.id).status == "REPAIR_REQUIRED"
        assert len(company.runs_for_work_order(work_order.id)) == 1

        source.snapshot = _snapshot(commit="d" * 40, tree="e" * 64)
        repair_run = company.repair_once(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="changed-repair",
            repair_manifest=manifest(),
        )
        assert repair_run.status == "PASS"
        assert company.work_order(work_order.id).status == "WAITING_FOR_OPUS"
        assert company.review(review.id).status == "CHANGES_REQUIRED"

        rereview_row = company.store.query_one(
            """
            SELECT * FROM reviews
            WHERE work_order_id = ? AND parent_review_id = ?
            """,
            (work_order.id, review.id),
        )
        assert rereview_row is not None
        rereview = company.review(rereview_row["id"])
        rerequest = read_json(rereview.json_path)
        assert rerequest["change_resolutions"]
        assert {
            row["status"]
            for row in company.store.query_all(
                "SELECT status FROM review_required_changes WHERE review_id = ?",
                (review.id,),
            )
        } == {"SUBMITTED"}

        pass_path = _write_result(
            tmp_path / "rereview-pass.json",
            _review_result(rereview, verdict="PASS"),
        )
        company.ingest_review_result(rereview.id, pass_path)

        assert company.work_order(work_order.id).status == "COMPLETED"
        assert {
            row["status"]
            for row in company.store.query_all(
                "SELECT status FROM review_required_changes WHERE review_id = ?",
                (review.id,),
            )
        } == {"VERIFIED"}
