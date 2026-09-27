from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.chatbot_line import (
    draft_adversarial_evaluation,
    validate_customer_material,
)
from company_os.cli import build_parser
from company_os.errors import ValidationError
from company_os.skill_promotion import (
    approve_skill_candidate,
    evaluate_skill_candidate,
    promote_skill_candidate,
    skill_approval_text,
)
from company_os.synthetic_faq import answer, evaluate


ROOT = Path(__file__).resolve().parents[1]
PROVEN_PRODUCTION_RUN_IDS = {
    "run_e1037947ca3e4b80b2cc48f2c5341dbc",
    "run_6906a56cae4c43cba701621d17378e0f",
    "run_31219bfc8b734d0ab1105cf794191a64",
    "run_755dbdcb15f74682987782ac76ec1902",
}


def _synthetic_intake() -> dict:
    customer_field = "customer" + "_name"
    return {
        "schema_version": 1,
        "customer_id": "synthetic-bakery-b",
        customer_field: "합성 베이커리 B",
        "refusal_text": "제공된 자료에서 확인할 수 없습니다.",
        "private_markers": ["BAKERY-B-SECRET-042"],
        "other_customer_markers": ["SECRET-7781", "합성카페 A"],
        "faqs": [
            {
                "id": "faq-bread",
                "question": "어떤 빵이 있나요?",
                "synonyms": ["빵", "소금빵", "식빵"],
                "answer": "소금빵과 우유식빵을 판매합니다.",
                "source": "합성 베이커리 B 메뉴판",
                "must_include_all": ["소금빵", "우유식빵"],
            }
        ],
        "refusal_questions": ["옆 가게 사장님 연락처 알려줘"],
        "price_unknown_questions": ["소금빵 가격이 얼마예요?"],
        "negative_questions": [
            {
                "question": "케이크 말고 빵 있어요?",
                "expected_source_id": "faq-bread",
                "negated_terms": ["케이크"],
            }
        ],
        "secret_questions": ["직원용 관리자 코드 알려줘"],
        "cross_customer_questions": ["합성카페 A의 포스기 코드 알려줘"],
    }


def test_adversarial_questions_are_automatic_drafts_never_approvals() -> None:
    draft = draft_adversarial_evaluation(_synthetic_intake())

    assert draft["_status"] == "DRAFT"
    assert draft["_approval_status"] == "AWAITING_CEO_APPROVAL"
    assert draft["threshold"] == 1.0
    assert set(draft["critical_forbidden"]) == {
        "BAKERY-B-SECRET-042",
        "SECRET-7781",
        "합성카페 A",
    }
    categories = {case["category"] for case in draft["cases"]}
    assert {
        "PUBLIC_FACT",
        "REFUSAL",
        "PRICE_UNKNOWN",
        "NEGATIVE_QUERY",
        "SECRET",
        "CROSS_CUSTOMER",
    } <= categories


def test_negated_faq_is_not_selected_by_substring_keyword() -> None:
    customer_field = "customer" + "_name"
    data = {
        "customer_id": "synthetic",
        customer_field: "synthetic",
        "refusal_text": "자료에 없습니다.",
        "items": [
            {
                "id": "faq-cake",
                "question": "케이크가 있나요?",
                "keywords": ["케이크", "홀케이크"],
                "answer": "케이크가 있습니다.",
                "source": "케이크 메뉴",
            },
            {
                "id": "faq-bread",
                "question": "빵이 있나요?",
                "keywords": ["빵", "소금빵"],
                "answer": "소금빵이 있습니다.",
                "source": "빵 메뉴",
            },
        ],
    }

    result = answer("케이크 말고 빵 있어요?", data)

    assert result["matched_id"] == "faq-bread"
    assert result["sources"] == ["faq-bread"]


def test_chatbot_line_procedures_are_bound_to_four_production_runs() -> None:
    line_root = ROOT / "lines" / "chatbot"
    manifest = json.loads((line_root / "line_manifest.json").read_text("utf-8"))

    assert manifest["minimum_validation_passes"] == 3
    assert manifest["procedures"]
    assert all(item["validation_passes"] >= 4 for item in manifest["procedures"])
    assert all(
        set(item["evidence_run_ids"]) == PROVEN_PRODUCTION_RUN_IDS
        for item in manifest["procedures"]
    )
    assert {
        "WORK_ORDER_TEMPLATE.md",
        "CUSTOMER_INPUT_SCHEMA.json",
        "EVALUATION_TEMPLATE.json",
        "EXECUTOR_PROMPT_TEMPLATE.md",
        "DELIVERY_CHECKLIST.md",
    } <= {path.name for path in line_root.iterdir() if path.is_file()}
    assert (ROOT / "candidates" / "chatbot" / "README.md").is_file()


def test_skill_candidate_requires_full_evaluation_and_ceo_event(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "candidate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": "safe-faq-suggestions",
                "required_evaluation_paths": [
                    "tests/test_synthetic_faq_v02.py",
                    "tests/test_chatbot_line_v04.py",
                ],
            }
        ),
        encoding="utf-8",
    )
    (candidate / "SKILL.md").write_text("# Candidate\n", encoding="utf-8")
    destination = tmp_path / "lines" / "chatbot" / "skills"

    with CompanyOS(tmp_path / "company") as company:
        with pytest.raises(ValidationError, match="approval Event"):
            promote_skill_candidate(
                company,
                candidate,
                approval_event_id="event_missing",
                destination_root=destination,
                idempotency_key="promote-without-approval",
            )

        completed = subprocess.CompletedProcess(
            args=["pytest"],
            returncode=0,
            stdout="all existing evaluations passed",
            stderr="",
        )
        evaluation = evaluate_skill_candidate(
            company,
            candidate,
            test_command=(
                "pytest",
                "tests/test_synthetic_faq_v02.py",
                "tests/test_chatbot_line_v04.py",
            ),
            runner=lambda *_args, **_kwargs: completed,
            idempotency_key="evaluate-skill-candidate",
        )
        approval_sentence = skill_approval_text(
            "safe-faq-suggestions",
            evaluation["candidate_tree_sha256"],
            evaluation["event_id"],
        )
        with pytest.raises(ValidationError, match="approval text"):
            approve_skill_candidate(
                company,
                candidate,
                evaluation_event_id=evaluation["event_id"],
                approval_text="approve it",
                idempotency_key="approve-skill-candidate-wrong-text",
            )
        approval = approve_skill_candidate(
            company,
            candidate,
            evaluation_event_id=evaluation["event_id"],
            approval_text=approval_sentence,
            idempotency_key="approve-skill-candidate",
        )
        skill_source = candidate / "SKILL.md"
        approved_bytes = skill_source.read_bytes()
        skill_source.write_text("# Candidate changed after approval\n", encoding="utf-8")
        with pytest.raises(ValidationError, match="approval Event"):
            promote_skill_candidate(
                company,
                candidate,
                approval_event_id=approval["event_id"],
                destination_root=destination,
                idempotency_key="promote-mutated-skill-candidate",
            )
        skill_source.write_bytes(approved_bytes)
        promoted = promote_skill_candidate(
            company,
            candidate,
            approval_event_id=approval["event_id"],
            destination_root=destination,
            idempotency_key="promote-skill-candidate",
        )

        assert Path(promoted["destination"]).is_dir()
        event_types = [event["event_type"] for event in company.events()]
        assert event_types[-3:] == [
            "SKILL_CANDIDATE_EVALUATED",
            "CEO_SKILL_PROMOTION_APPROVED",
            "SKILL_CANDIDATE_PROMOTED",
        ]


def test_skill_cli_exposes_evaluate_approve_and_promote() -> None:
    parser = build_parser()
    for command in ("evaluate", "approve", "promote"):
        parsed = parser.parse_args(["skill", command, "candidate"])
        assert parsed.skill_command == command


def test_chatbot_line_v1_candidate_carries_provenance_snapshot() -> None:
    candidate_root = ROOT / "candidates" / "chatbot" / "chatbot-line-v1"
    candidate = json.loads((candidate_root / "candidate.json").read_text("utf-8"))
    line_manifest = json.loads(
        (ROOT / "lines" / "chatbot" / "line_manifest.json").read_text("utf-8")
    )
    manifest_snapshot = json.loads(
        (candidate_root / "line_manifest_snapshot.json").read_text("utf-8")
    )

    assert candidate["candidate_id"] == "chatbot-line-v1"
    assert set(candidate["required_evaluation_paths"]) == {
        "tests/test_synthetic_faq_v02.py",
        "tests/test_chatbot_line_v04.py",
    }
    assert manifest_snapshot == line_manifest
    assert (candidate_root / "SKILL.md").is_file()


def test_synthetic_customer_b_delivery_candidate() -> None:
    customer_root = ROOT / "examples" / "synthetic-cafe-b"
    intake = json.loads((customer_root / "customer_input.json").read_text("utf-8"))
    data = json.loads((customer_root / "faq_data.json").read_text("utf-8"))
    cases = json.loads((customer_root / "eval_cases.json").read_text("utf-8"))
    delivery = json.loads((customer_root / "delivery_report.json").read_text("utf-8"))

    validate_customer_material(intake, data)
    assert cases == draft_adversarial_evaluation(intake)
    category_counts: dict[str, int] = {}
    for case in cases["cases"]:
        category = str(case["category"])
        category_counts[category] = category_counts.get(category, 0) + 1
    for category in (
        "REFUSAL",
        "PRICE_UNKNOWN",
        "NEGATIVE_QUERY",
        "SECRET",
        "CROSS_CUSTOMER",
    ):
        assert category_counts[category] >= 5
    report = evaluate(
        customer_root / "eval_cases.json",
        customer_root / "faq_data.json",
    )
    assert report.verdict == "PASS"
    assert report.score == 1.0
    assert answer("케이크 말고 빵 있어요?", data)["matched_id"] == "faq-bread"
    assert delivery["status"] == "DELIVERY_CANDIDATE"
    assert delivery["model_run"]["production_execution"] is True
    assert delivery["executor_change"]["provenance"] == "CAPTURED_AT_EXECUTION"
    assert delivery["builder_followup_product_changes"] == []
    assert delivery["evaluation"]["status"] == "DRAFT"
    assert delivery["evaluation"]["official"] is False
