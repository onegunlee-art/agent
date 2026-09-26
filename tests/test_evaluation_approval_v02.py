from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.cli import build_parser
from company_os.errors import ValidationError
from company_os.fakes import FakeExecutor
from company_os.synthetic_faq import evaluate

from .helpers import CleanSourceSnapshotter, build_venture


ROOT = Path(__file__).resolve().parents[1]
CAFE_A = ROOT / "examples" / "synthetic-cafe-a"


def _evaluation_files(root: Path) -> tuple[Path, Path]:
    target = root / "evaluation"
    target.mkdir()
    cases = target / "eval_cases.json"
    data = target / "faq_data.json"
    shutil.copyfile(CAFE_A / "eval_cases.json", cases)
    shutil.copyfile(CAFE_A / "faq_data.json", data)
    return cases, data


def _approval_text(digest: str) -> str:
    return (
        "V0.2 평가 사례 13건(eval_cases.json SHA-256: "
        f"{digest})과 threshold 1.00을 APPROVED로 승인합니다."
    )


def _prepared_company(tmp_path: Path):
    company = CompanyOS(
        tmp_path,
        source_snapshotter=CleanSourceSnapshotter(),
    ).initialize()
    _, _, _, work_order = build_venture(company, "evaluation-approval")
    run = company.execute_work_order(
        work_order.id,
        executor=FakeExecutor(),
        idempotency_key="evaluation-approval-run",
    )
    cases, data = _evaluation_files(tmp_path)
    digest = hashlib.sha256(cases.read_bytes()).hexdigest()
    return company, work_order, run, cases, data, digest


def test_evaluation_approval_rejects_hash_or_approval_text_mismatch(
    tmp_path: Path,
) -> None:
    company, work_order, _, cases, _, digest = _prepared_company(tmp_path)
    try:
        with pytest.raises(ValidationError, match="SHA-256"):
            company.record_evaluation_approval(
                work_order.id,
                cases_path=cases,
                expected_sha256="0" * 64,
                approval_text=_approval_text(digest),
                idempotency_key="wrong-hash",
            )
        with pytest.raises(ValidationError, match="approval text"):
            company.record_evaluation_approval(
                work_order.id,
                cases_path=cases,
                expected_sha256=digest,
                approval_text="승인합니다.",
                idempotency_key="wrong-text",
            )

        assert company.store.scalar(
            "SELECT COUNT(*) FROM approvals WHERE decision_id IS NOT NULL"
        ) == 0
        assert company.store.scalar(
            "SELECT COUNT(*) FROM events WHERE event_type = 'EVALUATION_SPEC_APPROVED'"
        ) == 0
    finally:
        company.close()


def test_evaluation_approval_is_canonical_and_keeps_draft_file_immutable(
    tmp_path: Path,
) -> None:
    company, work_order, _, cases, _, digest = _prepared_company(tmp_path)
    before = cases.read_bytes()
    try:
        result = company.record_evaluation_approval(
            work_order.id,
            cases_path=cases,
            expected_sha256=digest,
            approval_text=_approval_text(digest),
            idempotency_key="approve-exact-evaluation",
        )

        assert cases.read_bytes() == before
        assert json.loads(before)["_status"] == "DRAFT"
        approval = company.store.get_row("approvals", result["approval_id"])
        decision = company.store.get_row("decisions", result["decision_id"])
        assert approval is not None and approval["status"] == "APPROVED"
        assert approval["actor"] == "CEO"
        assert decision is not None and decision["status"] == "RESOLVED"
        payload = json.loads(decision["payload_json"])
        assert payload["classification"] == "EVALUATION_SPEC_APPROVAL"
        assert payload["evaluation_spec_sha256"] == digest
        assert payload["case_count"] == 13
        assert payload["threshold"] == 1.0
        event = company.store.query_one(
            "SELECT payload_json FROM events "
            "WHERE event_type = 'EVALUATION_SPEC_APPROVED'"
        )
        assert event is not None
        assert json.loads(event["payload_json"])["approval_id"] == result["approval_id"]
    finally:
        company.close()


def test_official_evaluation_requires_current_hash_bound_approval(
    tmp_path: Path,
) -> None:
    company, work_order, run, cases, data, digest = _prepared_company(tmp_path)
    try:
        approval = company.record_evaluation_approval(
            work_order.id,
            cases_path=cases,
            expected_sha256=digest,
            approval_text=_approval_text(digest),
            idempotency_key="approve-before-stale",
        )
        cases.write_text(cases.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        with pytest.raises(ValidationError, match="changed after CEO approval"):
            company.run_official_evaluation(
                work_order.id,
                run.id,
                cases_path=cases,
                data_path=data,
                approval_id=approval["approval_id"],
                idempotency_key="stale-official-evaluation",
            )
        assert company.store.scalar(
            "SELECT COUNT(*) FROM evidence WHERE kind = 'RUBRIC_REPORT'"
        ) == 0
    finally:
        company.close()


def test_official_evaluation_is_bound_to_approval_and_clean_source(
    tmp_path: Path,
) -> None:
    company, work_order, run, cases, data, digest = _prepared_company(tmp_path)
    try:
        approval = company.record_evaluation_approval(
            work_order.id,
            cases_path=cases,
            expected_sha256=digest,
            approval_text=_approval_text(digest),
            idempotency_key="approve-for-official-evaluation",
        )
        evidence = company.run_official_evaluation(
            work_order.id,
            run.id,
            cases_path=cases,
            data_path=data,
            approval_id=approval["approval_id"],
            idempotency_key="official-evaluation",
        )

        assert evidence.kind == "RUBRIC_REPORT"
        row = company.store.get_row("evidence", evidence.id)
        payload = json.loads(row["payload_json"])
        assert payload["official"] is True
        assert payload["evaluation_status"] == "APPROVED"
        assert payload["evaluation_approval_id"] == approval["approval_id"]
        assert payload["evaluation_spec_sha256"] == digest
        assert payload["evaluation_data_path"] == "evaluation/faq_data.json"
        assert payload["evaluation_data_sha256"] == hashlib.sha256(
            data.read_bytes()
        ).hexdigest()
        assert payload["case_count"] == 13
        assert payload["score"] == payload["threshold"] == 1.0
        assert payload["source_commit"] == "a" * 40
        assert payload["source_tree_sha256"] == "c" * 64
        report = json.loads(evidence.path.read_text(encoding="utf-8"))
        assert report["official"] is True
        assert report["evaluation_approval_id"] == approval["approval_id"]
        assert report["evaluation_data_sha256"] == hashlib.sha256(
            data.read_bytes()
        ).hexdigest()
        assert len(report["cases"]) == 13
        event = company.store.query_one(
            "SELECT payload_json FROM events "
            "WHERE event_type = 'RUBRIC_EVALUATED' AND aggregate_id = ?",
            (run.id,),
        )
        assert json.loads(event["payload_json"])["official"] is True
    finally:
        company.close()


def test_caller_cannot_self_declare_an_official_rubric_report(tmp_path: Path) -> None:
    company, work_order, run, cases, data, _ = _prepared_company(tmp_path)
    try:
        report = evaluate(cases, data)
        with pytest.raises(ValidationError, match="canonical CEO approval"):
            company.record_rubric_report(
                work_order.id,
                run.id,
                report,
                evaluation_status="APPROVED",
                idempotency_key="self-declared-official",
            )
    finally:
        company.close()


def test_official_report_never_overwrites_prior_draft_evidence(tmp_path: Path) -> None:
    company, work_order, run, cases, data, digest = _prepared_company(tmp_path)
    try:
        report = evaluate(cases, data)
        draft = company.record_rubric_report(
            work_order.id,
            run.id,
            report,
            idempotency_key="draft-before-official",
        )
        draft_path = draft.path
        draft_bytes = draft_path.read_bytes()
        approval = company.record_evaluation_approval(
            work_order.id,
            cases_path=cases,
            expected_sha256=digest,
            approval_text=_approval_text(digest),
            idempotency_key="approve-after-draft",
        )

        official = company.run_official_evaluation(
            work_order.id,
            run.id,
            cases_path=cases,
            data_path=data,
            approval_id=approval["approval_id"],
            idempotency_key="official-after-draft",
        )

        assert official.path != draft_path
        assert draft_path.read_bytes() == draft_bytes
        assert hashlib.sha256(draft_bytes).hexdigest() == draft.sha256
        assert hashlib.sha256(official.path.read_bytes()).hexdigest() == official.sha256
    finally:
        company.close()


def test_official_evaluation_cli_exposes_approval_and_run_commands() -> None:
    parser = build_parser()
    approval = parser.parse_args(
        [
            "evaluation",
            "approve",
            "work_order_1",
            "--cases",
            "eval_cases.json",
            "--expected-sha256",
            "a" * 64,
            "--approval-file",
            "approval.txt",
            "--idempotency-key",
            "approve-1",
        ]
    )
    official = parser.parse_args(
        [
            "evaluation",
            "run",
            "work_order_1",
            "--run",
            "run_1",
            "--cases",
            "eval_cases.json",
            "--data",
            "faq_data.json",
            "--approval",
            "approval_1",
            "--idempotency-key",
            "official-1",
        ]
    )

    assert approval.evaluation_command == "approve"
    assert official.evaluation_command == "run"
