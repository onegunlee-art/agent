from __future__ import annotations

import hashlib
import json
from pathlib import Path

from company_os.application import CompanyOS
from company_os.cli import build_parser
from company_os.pilot import (
    create_delivery_report,
    evaluation_approval_text_v2,
)
from company_os.utils import canonical_json, sha256_file

from .helpers import CleanSourceSnapshotter, build_venture


ROOT = Path(__file__).resolve().parents[1]


def test_customer_pilot_documents_are_printable_and_cover_required_controls() -> None:
    documents = {
        "pilot/templates/PILOT_CONSENT_KO.md": (
            "자료 사용 범위",
            "보관",
            "삭제 요청",
            "롤백",
        ),
        "pilot/templates/CUSTOMER_INPUT_SHEET_KO.md": (
            "FAQ",
            "답하면 안 되는 질문",
            "거절해야 할 질문",
        ),
        "pilot/templates/LOCAL_DEMO_GUIDE_KO.md": (
            "localhost",
            "알려진 한계",
            "수정 요청",
        ),
    }
    for relative, required in documents.items():
        content = (ROOT / relative).read_text(encoding="utf-8")
        assert len(content.splitlines()) <= 80
        assert all(value in content for value in required)
    runbook = (ROOT / "pilot/PILOT_RUNBOOK_KO.md").read_text(encoding="utf-8")
    for required in (
        "등록",
        "자료 입력",
        "라인 실행",
        "평가 승인",
        "로컬 시연",
        "수정 반복 상한 3회",
        "백업",
        "삭제",
        "250,000",
        "20분",
    ):
        assert required in runbook


def test_evaluation_approval_v2_binds_customer_and_repo_relative_spec_path(
    tmp_path: Path,
) -> None:
    company = CompanyOS(
        tmp_path,
        source_snapshotter=CleanSourceSnapshotter(),
    ).initialize()
    try:
        _, _, _, work_order = build_venture(company, "pilot-approval-v2")
        cases_path = tmp_path / "examples" / "synthetic-cafe-c" / "eval_cases.json"
        cases_path.parent.mkdir(parents=True)
        cases_path.write_text(
            json.dumps(
                {
                    "_status": "DRAFT",
                    "threshold": 1.0,
                    "cases": [{"id": "q01"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        digest = sha256_file(cases_path)
        relative = "examples/synthetic-cafe-c/eval_cases.json"
        approval_text = evaluation_approval_text_v2(
            customer_id="synthetic-cafe-c",
            spec_path=relative,
            case_count=1,
            spec_sha256=digest,
            threshold=1.0,
        )

        result = company.record_evaluation_approval(
            work_order.id,
            cases_path=cases_path,
            expected_sha256=digest,
            approval_text=approval_text,
            customer_id="synthetic-cafe-c",
            idempotency_key="pilot-evaluation-v2",
        )

        assert result["approval_template_version"] == 2
        event = company.events()[-1]
        assert event["event_type"] == "EVALUATION_SPEC_APPROVED"
        assert event["payload"]["customer_id"] == "synthetic-cafe-c"
        assert event["payload"]["evaluation_spec_path"] == relative
        assert event["payload"]["approval_template_version"] == 2
    finally:
        company.close()


def test_evaluation_approve_cli_accepts_customer_id() -> None:
    parsed = build_parser().parse_args(
        [
            "evaluation",
            "approve",
            "work-order-1",
            "--cases",
            "eval_cases.json",
            "--expected-sha256",
            "a" * 64,
            "--approval-file",
            "approval.txt",
            "--customer-id",
            "synthetic-cafe-c",
            "--idempotency-key",
            "approval-v2",
        ]
    )
    assert parsed.customer_id == "synthetic-cafe-c"


def test_delivery_report_binds_source_evidence_approvals_review_and_manifest(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "chatbot-result.json"
    artifact.write_text('{"synthetic":true}\n', encoding="utf-8")
    output = tmp_path / "delivery_report.json"

    report = create_delivery_report(
        output,
        customer_id="synthetic-cafe-c",
        source_commit="a" * 40,
        source_tree_oid="b" * 40,
        model_run_id="run_synthetic_c",
        model_execution_id="execution_synthetic_c",
        evaluation_evidence_id="evidence_synthetic_c",
        evaluation_report_sha256="c" * 64,
        evaluation_approval_event_id="event_eval_approval",
        skill_promotion_event_id="event_skill_promoted",
        review_verdict_sha256="d" * 64,
        review_provenance="USER_SUPPLIED_TRANSCRIPT",
        artifacts=[("chatbot-result.json", artifact)],
    )

    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted == report
    assert persisted["customer_id"] == "synthetic-cafe-c"
    assert persisted["source"]["commit"] == "a" * 40
    assert persisted["evaluation"]["evidence_id"] == "evidence_synthetic_c"
    assert persisted["approvals"]["evaluation_event_id"] == "event_eval_approval"
    assert persisted["review"]["provenance"] == "USER_SUPPLIED_TRANSCRIPT"
    assert persisted["manifest"]["files"][0]["sha256"] == sha256_file(artifact)
    manifest_core = {"files": persisted["manifest"]["files"]}
    assert persisted["manifest"]["sha256"] == hashlib.sha256(
        canonical_json(manifest_core).encode("utf-8")
    ).hexdigest()
    assert persisted["signature_envelope"]["format"] == "DSSE_COMPATIBLE_UNSIGNED"
