from __future__ import annotations

from pathlib import Path

from company_os.synthetic_faq import answer, evaluate, load_json


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "synthetic-cafe-c"


def test_synthetic_customer_c_pet_guidance_is_source_bound() -> None:
    data = load_json(EXAMPLE / "faq_data.json")

    response = answer("강아지와 같이 갈 수 있나요?", data)

    assert response["refused"] is False
    assert response["matched_id"] == "faq-pet"
    assert response["sources"] == ["faq-pet"]
    assert "이동 가방" in response["answer_text"]
    assert "테라스" in response["answer_text"]
    assert "SYNTHETIC-C-POS-CODE-73" not in response["text"]


def test_synthetic_customer_c_draft_evaluation_is_perfect() -> None:
    report = evaluate(EXAMPLE / "eval_cases.json", EXAMPLE / "faq_data.json")

    assert report.verdict == "PASS"
    assert report.score == 1.0
    assert len(report.cases) == 6
