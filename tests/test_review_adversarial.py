from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

import pytest

from company_os.application import CompanyOS
from company_os.errors import ValidationError
from company_os.fakes import FakeExecutor
from company_os.source_snapshot import SourceSnapshot
from company_os.utils import atomic_write_json, read_json, sha256_file

from .helpers import build_venture


def _snapshot(
    commit: str = "a" * 40,
    tree: str = "b" * 64,
    *,
    dirty: bool = False,
) -> SourceSnapshot:
    return SourceSnapshot(
        source_commit=commit,
        source_tree_oid="c" * 40,
        source_tree_sha256=tree,
        dirty=dirty,
    )


@dataclass
class MutableSnapshot:
    snapshot: SourceSnapshot
    flip_after_next_capture: SourceSnapshot | None = None

    def capture(self) -> SourceSnapshot:
        captured = self.snapshot
        if self.flip_after_next_capture is not None:
            self.snapshot = self.flip_after_next_capture
            self.flip_after_next_capture = None
        return captured


def _write_result(path: Path, payload: dict) -> Path:
    atomic_write_json(path, payload)
    return path


def _result(
    review,
    *,
    verdict: str,
    changes: list[dict] | None = None,
    schema_version: int = 2,
) -> dict:
    request = read_json(review.json_path)
    return {
        "schema_version": schema_version,
        "review_request_id": review.id,
        "review_request_hash": review.request_hash,
        "reviewed_commit": request["source_commit"],
        "reviewed_tree_sha256": request["source_tree_sha256"],
        "source": "user_supplied",
        "verdict": verdict,
        "findings": [],
        "required_changes": changes or [],
    }


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


def _require_change(company: CompanyOS, root: Path, review) -> list[dict]:
    changes = [{"id": "CHANGE-X", "description": "Apply the requested repair."}]
    company.ingest_review_result(
        review.id,
        _write_result(
            root / "changes-required.json",
            _result(review, verdict="CHANGES_REQUIRED", changes=changes),
        ),
    )
    return changes


def _manifest(
    company: CompanyOS,
    review,
    source: MutableSnapshot,
    evidence_id: str,
    evidence_path: Path,
    *,
    change_id: str = "CHANGE-X",
) -> dict:
    # The legacy path/hash member makes this same test exercise the vulnerable
    # implementation before the Evidence-ID-only schema is installed.
    return {
        "schema_version": 1,
        "review_id": review.id,
        "source_commit": source.snapshot.source_commit,
        "source_tree_sha256": source.snapshot.source_tree_sha256,
        "changes": [
            {
                "id": change_id,
                "commit": source.snapshot.source_commit,
                "evidence_ids": [evidence_id],
                "evidence": [
                    {
                        "path": str(evidence_path),
                        "sha256": sha256_file(evidence_path),
                    }
                ],
            }
        ],
    }


def test_dirty_source_cannot_create_review_request(tmp_path: Path) -> None:
    source = MutableSnapshot(_snapshot(dirty=True))
    with CompanyOS(tmp_path, source_snapshotter=source) as company:
        _, _, _, work_order = build_venture(company, "dirty-review")
        company.execute_work_order(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="dirty-review-run",
        )

        with pytest.raises(ValidationError, match="clean"):
            company.prepare_review(
                work_order.id,
                idempotency_key="dirty-review-request",
            )

        assert company.work_order(work_order.id).status == "VERIFIED"


def test_public_execute_cannot_bypass_repair_validation(tmp_path: Path) -> None:
    source = MutableSnapshot(_snapshot())
    with CompanyOS(tmp_path, source_snapshotter=source) as company:
        work_order, _, review = _verified_review(company, "direct-repair-bypass")
        _require_change(company, tmp_path, review)
        forged = {
            "schema_version": 1,
            "review_id": review.id,
            "source_commit": "0" * 40,
            "source_tree_sha256": "1" * 64,
            "changes": [
                {
                    "id": "NOT-THE-REQUIRED-ID",
                    "commit": "0" * 40,
                    "evidence": [{"path": "missing", "sha256": "2" * 64}],
                }
            ],
        }

        with pytest.raises(TypeError):
            company.execute_work_order(
                work_order.id,
                executor=FakeExecutor(),
                idempotency_key="direct-repair-bypass",
                _repair_mode=True,
                _repair_manifest=forged,
            )

        assert company.work_order(work_order.id).status == "REPAIR_REQUIRED"


def test_internal_execute_revalidates_repair_manifest(tmp_path: Path) -> None:
    source = MutableSnapshot(_snapshot())
    with CompanyOS(tmp_path, source_snapshotter=source) as company:
        work_order, run, review = _verified_review(company, "internal-repair-bypass")
        _require_change(company, tmp_path, review)
        evidence = next(
            item
            for item in company.evidence_for_run(run.id)
            if item.kind == "VERIFIER_OUTPUT"
        )
        unchanged_manifest = _manifest(
            company,
            review,
            source,
            evidence.id,
            evidence.path,
        )

        with pytest.raises(ValidationError, match="no substantive change"):
            company._execute_work_order(
                work_order.id,
                executor=FakeExecutor(),
                idempotency_key="internal-repair-bypass",
                _repair_mode=True,
                _repair_manifest=unchanged_manifest,
            )

        assert company.work_order(work_order.id).status == "REPAIR_REQUIRED"


def test_repair_manifest_rejects_noncanonical_evidence_id(tmp_path: Path) -> None:
    source = MutableSnapshot(_snapshot())
    with CompanyOS(tmp_path, source_snapshotter=source) as company:
        work_order, run, review = _verified_review(company, "forged-evidence")
        _require_change(company, tmp_path, review)
        source.snapshot = _snapshot("d" * 40, "e" * 64)
        evidence = next(
            item
            for item in company.evidence_for_run(run.id)
            if item.kind == "VERIFIER_OUTPUT"
        )
        manifest = _manifest(
            company,
            review,
            source,
            "evidence_does_not_exist",
            evidence.path,
        )

        with pytest.raises(ValidationError, match="Evidence"):
            company.repair_once(
                work_order.id,
                executor=FakeExecutor(),
                idempotency_key="forged-evidence-repair",
                repair_manifest=manifest,
            )

        assert company.work_order(work_order.id).status == "REPAIR_REQUIRED"


def test_resume_rejects_resolution_from_a_reverted_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _snapshot()
    repaired = _snapshot("d" * 40, "e" * 64)
    source = MutableSnapshot(original)
    with CompanyOS(tmp_path, source_snapshotter=source) as company:
        work_order, run, review = _verified_review(company, "reverted-repair")
        _require_change(company, tmp_path, review)
        source.snapshot = repaired
        evidence = next(
            item
            for item in company.evidence_for_run(run.id)
            if item.kind == "VERIFIER_OUTPUT"
        )
        manifest = _manifest(company, review, source, evidence.id, evidence.path)

        original_prepare = company.prepare_review

        def simulated_crash(*args, **kwargs):
            raise RuntimeError("simulated crash before rereview request")

        monkeypatch.setattr(company, "prepare_review", simulated_crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            company.repair_once(
                work_order.id,
                executor=FakeExecutor(),
                idempotency_key="reverted-repair-second-run",
                repair_manifest=manifest,
            )
        monkeypatch.setattr(company, "prepare_review", original_prepare)
        assert company.work_order(work_order.id).status == "AWAITING_REREVIEW"

        source.snapshot = original
        with pytest.raises(ValidationError, match="resolution source"):
            company.resume_work_order(work_order.id, executor=FakeExecutor())

        assert company.work_order(work_order.id).status == "AWAITING_REREVIEW"


def test_source_change_during_ingest_rejects_stale_result(tmp_path: Path) -> None:
    source = MutableSnapshot(_snapshot())
    with CompanyOS(tmp_path, source_snapshotter=source) as company:
        work_order, _, review = _verified_review(company, "stale-race")
        source.flip_after_next_capture = _snapshot("d" * 40, "e" * 64)
        result_path = _write_result(
            tmp_path / "stale-pass.json",
            _result(review, verdict="PASS"),
        )

        with pytest.raises(ValidationError, match="stale"):
            company.ingest_review_result(review.id, result_path)

        assert company.work_order(work_order.id).status == "WAITING_FOR_OPUS"

        replacement = company.resume_work_order(
            work_order.id,
            executor=FakeExecutor(),
        )
        assert replacement.id != review.id
        assert replacement.status == "WAITING_FOR_OPUS"
        assert read_json(replacement.json_path)["source_commit"] == "d" * 40
        assert company.review(review.id).status == "SUPERSEDED"
        assert any(
            event["event_type"] == "OPUS_REVIEW_REQUEST_SUPERSEDED"
            and event["aggregate_id"] == review.id
            for event in company.events()
        )

        company.ingest_review_result(
            replacement.id,
            _write_result(
                tmp_path / "replacement-pass.json",
                _result(replacement, verdict="PASS"),
            ),
        )
        assert company.work_order(work_order.id).status == "COMPLETED"


def test_source_change_during_rereview_reopens_and_can_be_repaired_again(
    tmp_path: Path,
) -> None:
    source = MutableSnapshot(_snapshot())
    with CompanyOS(tmp_path, source_snapshotter=source) as company:
        work_order, initial_run, parent_review = _verified_review(
            company,
            "rereview-source-change",
        )
        _require_change(company, tmp_path, parent_review)
        source.snapshot = _snapshot("d" * 40, "e" * 64)
        initial_evidence = next(
            item
            for item in company.evidence_for_run(initial_run.id)
            if item.kind == "VERIFIER_OUTPUT"
        )
        first_repair = company.repair_once(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="rereview-source-change-first-repair",
            repair_manifest=_manifest(
                company,
                parent_review,
                source,
                initial_evidence.id,
                initial_evidence.path,
            ),
        )
        first_child_row = company.store.query_one(
            """
            SELECT * FROM reviews
            WHERE work_order_id = ? AND status = 'WAITING_FOR_OPUS'
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (work_order.id,),
        )
        assert first_child_row is not None

        source.snapshot = _snapshot("f" * 40, "1" * 64)
        with pytest.raises(ValidationError, match="new repair manifest"):
            company.resume_work_order(work_order.id, executor=FakeExecutor())
        assert company.work_order(work_order.id).status == "REPAIR_REQUIRED"
        assert company.review(first_child_row["id"]).status == "SUPERSEDED"
        change_row = company.store.query_one(
            """
            SELECT status FROM review_required_changes
            WHERE review_id = ? AND change_id = 'CHANGE-X'
            """,
            (parent_review.id,),
        )
        assert change_row["status"] == "OPEN"

        repair_evidence = next(
            item
            for item in company.evidence_for_run(first_repair.id)
            if item.kind == "VERIFIER_OUTPUT"
        )
        second_repair = company.resume_work_order(
            work_order.id,
            executor=FakeExecutor(),
            repair_manifest=_manifest(
                company,
                parent_review,
                source,
                repair_evidence.id,
                repair_evidence.path,
            ),
        )
        final_child_row = company.store.query_one(
            """
            SELECT id FROM reviews
            WHERE work_order_id = ? AND status = 'WAITING_FOR_OPUS'
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (work_order.id,),
        )
        assert final_child_row is not None
        final_child = company.review(final_child_row["id"])
        assert read_json(final_child.json_path)["source_commit"] == "f" * 40

        company.ingest_review_result(
            final_child.id,
            _write_result(
                tmp_path / "rereview-source-change-more.json",
                _result(
                    final_child,
                    verdict="CHANGES_REQUIRED",
                    changes=[
                        {
                            "id": "CHANGE-Y",
                            "description": "Verify repeated lineage repair.",
                        }
                    ],
                ),
            ),
        )
        second_repair_evidence = next(
            item
            for item in company.evidence_for_run(second_repair.id)
            if item.kind == "VERIFIER_OUTPUT"
        )
        source.snapshot = _snapshot("2" * 40, "3" * 64)
        company.resume_work_order(
            work_order.id,
            executor=FakeExecutor(),
            repair_manifest=_manifest(
                company,
                final_child,
                source,
                second_repair_evidence.id,
                second_repair_evidence.path,
                change_id="CHANGE-Y",
            ),
        )
        last_child_row = company.store.query_one(
            """
            SELECT id FROM reviews
            WHERE work_order_id = ? AND status = 'WAITING_FOR_OPUS'
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (work_order.id,),
        )
        assert last_child_row is not None
        last_child = company.review(last_child_row["id"])
        lineage_request = read_json(last_child.json_path)
        assert lineage_request["review_lineage"] == [
            final_child.id,
            parent_review.id,
        ]
        company.ingest_review_result(
            last_child.id,
            _write_result(
                tmp_path / "rereview-source-change-pass.json",
                _result(last_child, verdict="PASS"),
            ),
        )
        assert company.work_order(work_order.id).status == "COMPLETED"


def test_bound_artifact_change_before_ingest_rejects_result(tmp_path: Path) -> None:
    source = MutableSnapshot(_snapshot())
    with CompanyOS(tmp_path, source_snapshotter=source) as company:
        work_order, run, review = _verified_review(company, "artifact-after-request")
        artifact = next(
            item
            for item in company.evidence_for_run(run.id)
            if item.kind == "RUN_ARTIFACT"
        )
        artifact.path.write_text("tampered after request\n", encoding="utf-8")
        result_path = _write_result(
            tmp_path / "artifact-after-request-pass.json",
            _result(review, verdict="PASS"),
        )

        with pytest.raises(ValidationError, match="bound Evidence or Artifact"):
            company.ingest_review_result(review.id, result_path)

        assert company.work_order(work_order.id).status == "WAITING_FOR_OPUS"


def test_exact_repair_replay_returns_stored_run_after_source_changes(
    tmp_path: Path,
) -> None:
    source = MutableSnapshot(_snapshot())
    with CompanyOS(tmp_path, source_snapshotter=source) as company:
        work_order, run, review = _verified_review(company, "repair-replay")
        _require_change(company, tmp_path, review)
        source.snapshot = _snapshot("d" * 40, "e" * 64)
        evidence = next(
            item
            for item in company.evidence_for_run(run.id)
            if item.kind == "VERIFIER_OUTPUT"
        )
        manifest = _manifest(company, review, source, evidence.id, evidence.path)
        key = "repair-replay-key"
        repaired = company.repair_once(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key=key,
            repair_manifest=manifest,
        )

        source.snapshot = _snapshot("f" * 40, "1" * 64)
        replayed = company.repair_once(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key=key,
            repair_manifest=manifest,
        )

        assert replayed.id == repaired.id
        assert len(company.runs_for_work_order(work_order.id)) == 2


def test_only_latest_changes_required_review_can_be_repaired(tmp_path: Path) -> None:
    source = MutableSnapshot(_snapshot())
    with CompanyOS(tmp_path, source_snapshotter=source) as company:
        work_order, run, review_one = _verified_review(company, "latest-review")
        _require_change(company, tmp_path, review_one)
        source.snapshot = _snapshot("d" * 40, "e" * 64)
        evidence_one = next(
            item
            for item in company.evidence_for_run(run.id)
            if item.kind == "VERIFIER_OUTPUT"
        )
        manifest_one = _manifest(
            company, review_one, source, evidence_one.id, evidence_one.path
        )
        repair_one = company.repair_once(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="latest-review-repair-one",
            repair_manifest=manifest_one,
        )
        review_two = company.review(
            company.store.query_one(
                "SELECT id FROM reviews WHERE work_order_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (work_order.id,),
            )["id"]
        )
        change_two = [{"id": "CHANGE-Y", "description": "Second repair."}]
        company.ingest_review_result(
            review_two.id,
            _write_result(
                tmp_path / "latest-review-second-changes.json",
                _result(
                    review_two,
                    verdict="CHANGES_REQUIRED",
                    changes=change_two,
                ),
            ),
        )
        source.snapshot = _snapshot("f" * 40, "1" * 64)

        with pytest.raises(ValidationError, match="latest CHANGES_REQUIRED"):
            company.repair_once(
                work_order.id,
                executor=FakeExecutor(),
                idempotency_key="latest-review-old-manifest",
                repair_manifest=manifest_one,
            )

        assert repair_one.status == "PASS"
        assert company.work_order(work_order.id).status == "REPAIR_REQUIRED"


def test_final_rereview_pass_verifies_all_ancestor_changes(tmp_path: Path) -> None:
    source = MutableSnapshot(_snapshot())
    with CompanyOS(tmp_path, source_snapshotter=source) as company:
        work_order, initial_run, review_one = _verified_review(
            company, "review-lineage"
        )
        _require_change(company, tmp_path, review_one)
        source.snapshot = _snapshot("d" * 40, "e" * 64)
        initial_evidence = next(
            item
            for item in company.evidence_for_run(initial_run.id)
            if item.kind == "VERIFIER_OUTPUT"
        )
        repair_one = company.repair_once(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="review-lineage-repair-one",
            repair_manifest=_manifest(
                company,
                review_one,
                source,
                initial_evidence.id,
                initial_evidence.path,
            ),
        )
        review_two_id = company.store.query_one(
            "SELECT id FROM reviews WHERE work_order_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (work_order.id,),
        )["id"]
        review_two = company.review(review_two_id)
        company.ingest_review_result(
            review_two.id,
            _write_result(
                tmp_path / "review-lineage-second-changes.json",
                _result(
                    review_two,
                    verdict="CHANGES_REQUIRED",
                    changes=[{"id": "CHANGE-Y", "description": "Second repair."}],
                ),
            ),
        )
        source.snapshot = _snapshot("f" * 40, "1" * 64)
        repair_one_evidence = next(
            item
            for item in company.evidence_for_run(repair_one.id)
            if item.kind == "VERIFIER_OUTPUT"
        )
        company.repair_once(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="review-lineage-repair-two",
            repair_manifest=_manifest(
                company,
                review_two,
                source,
                repair_one_evidence.id,
                repair_one_evidence.path,
                change_id="CHANGE-Y",
            ),
        )
        review_three_id = company.store.query_one(
            "SELECT id FROM reviews WHERE work_order_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (work_order.id,),
        )["id"]
        review_three = company.review(review_three_id)
        request_three = read_json(review_three.json_path)
        assert request_three["review_lineage"] == [review_two.id, review_one.id]
        assert {
            item["origin_review_id"]
            for item in request_three["change_resolutions"]
        } == {review_one.id, review_two.id}
        assert {
            item["required_change_id"]: item["description"]
            for item in request_three["change_resolutions"]
        } == {
            "CHANGE-X": "Apply the requested repair.",
            "CHANGE-Y": "Second repair.",
        }
        referenced_evidence_ids = {
            evidence_id
            for resolution in request_three["change_resolutions"]
            for evidence_id in resolution["evidence_ids"]
        }
        embedded_evidence_ids = {
            item["id"] for item in request_three["evidence"]
        }
        assert referenced_evidence_ids <= embedded_evidence_ids
        assert all(
            item.get("inline") is True or item.get("attachment_sha256")
            for item in request_three["evidence"]
        )
        result_path = _write_result(
            tmp_path / "review-lineage-pass.json",
            _result(review_three, verdict="PASS"),
        )
        ancestor_evidence_bytes = initial_evidence.path.read_bytes()
        initial_evidence.path.write_text(
            "tampered ancestor verifier output\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match="bound Evidence or Artifact"):
            company.ingest_review_result(review_three.id, result_path)
        initial_evidence.path.write_bytes(ancestor_evidence_bytes)
        company.ingest_review_result(
            review_three.id,
            result_path,
        )

        statuses = company.store.query_all(
            "SELECT change_id, status FROM review_required_changes "
            "ORDER BY change_id"
        )
        assert [(row["change_id"], row["status"]) for row in statuses] == [
            ("CHANGE-X", "VERIFIED"),
            ("CHANGE-Y", "VERIFIED"),
        ]
        assert company.work_order(work_order.id).status == "COMPLETED"


def test_v1_result_cannot_complete_v2_request(tmp_path: Path) -> None:
    source = MutableSnapshot(_snapshot())
    with CompanyOS(tmp_path, source_snapshotter=source) as company:
        work_order, _, review = _verified_review(company, "v1-result-v2-request")
        result_path = _write_result(
            tmp_path / "v1-pass.json",
            _result(review, verdict="PASS", schema_version=1),
        )

        with pytest.raises(ValidationError, match="schema_version"):
            company.ingest_review_result(review.id, result_path)

        assert company.work_order(work_order.id).status == "WAITING_FOR_OPUS"


def test_legacy_applied_repair_migrates_to_repairable_state(tmp_path: Path) -> None:
    source = MutableSnapshot(_snapshot())
    company = CompanyOS(tmp_path, source_snapshotter=source).initialize()
    work_order, _, review = _verified_review(company, "legacy-applied")
    _require_change(company, tmp_path, review)
    company.close()

    connection = sqlite3.connect(tmp_path / "var" / "state" / "company.db")
    connection.execute(
        "UPDATE work_orders SET status = 'REPAIRED_VERIFIED' WHERE id = ?",
        (work_order.id,),
    )
    connection.execute(
        """
        UPDATE reviews
        SET status = 'CHANGES_APPLIED', schema_version = 1,
            source_commit = NULL, source_tree_sha256 = NULL
        WHERE id = ?
        """,
        (review.id,),
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    with CompanyOS(tmp_path, source_snapshotter=source) as restored:
        row = restored.store.get_row("reviews", review.id)
        assert restored.work_order(work_order.id).status == "REPAIR_REQUIRED"
        assert row["status"] == "CHANGES_REQUIRED"
        assert row["binding_status"] == "LEGACY_UNBOUND"


def test_legacy_completed_is_preserved_but_marked_unbound(tmp_path: Path) -> None:
    source = MutableSnapshot(_snapshot())
    company = CompanyOS(tmp_path, source_snapshotter=source).initialize()
    work_order, _, review = _verified_review(company, "legacy-completed")
    company.close()

    database = tmp_path / "var" / "state" / "company.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE work_orders SET status = 'COMPLETED' WHERE id = ?",
        (work_order.id,),
    )
    connection.execute(
        """
        UPDATE reviews
        SET status = 'COMPLETED', schema_version = 1,
            source_commit = NULL, source_tree_sha256 = NULL
        WHERE id = ?
        """,
        (review.id,),
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    with CompanyOS(tmp_path, source_snapshotter=source) as restored:
        row = restored.store.get_row("reviews", review.id)
        assert restored.work_order(work_order.id).status == "COMPLETED"
        assert row["status"] == "COMPLETED"
        assert row["binding_status"] == "LEGACY_UNBOUND"


def test_legacy_waiting_review_migrates_and_reissues_as_bound_v2(
    tmp_path: Path,
) -> None:
    source = MutableSnapshot(_snapshot())
    company = CompanyOS(tmp_path, source_snapshotter=source).initialize()
    work_order, _, legacy_review = _verified_review(company, "legacy-waiting")
    company.close()

    database = tmp_path / "var" / "state" / "company.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        UPDATE reviews
        SET schema_version = 1, source_commit = NULL,
            source_tree_sha256 = NULL, binding_status = 'LEGACY_UNBOUND'
        WHERE id = ?
        """,
        (legacy_review.id,),
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    with CompanyOS(tmp_path, source_snapshotter=source) as restored:
        old_row = restored.store.get_row("reviews", legacy_review.id)
        assert old_row["status"] == "SUPERSEDED"
        assert old_row["binding_status"] == "LEGACY_UNBOUND"
        assert restored.work_order(work_order.id).status == "VERIFIED"

        replacement = restored.resume_work_order(
            work_order.id,
            executor=FakeExecutor(),
        )
        replacement_row = restored.store.get_row("reviews", replacement.id)
        assert replacement.id != legacy_review.id
        assert replacement_row["schema_version"] == 2
        assert replacement_row["binding_status"] == "BOUND"
        assert replacement.status == "WAITING_FOR_OPUS"

        restored.ingest_review_result(
            replacement.id,
            _write_result(
                tmp_path / "legacy-replacement-pass.json",
                _result(replacement, verdict="PASS"),
            ),
        )
        assert restored.work_order(work_order.id).status == "COMPLETED"
