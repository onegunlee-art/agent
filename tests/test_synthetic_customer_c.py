from __future__ import annotations

import hashlib
import json
from pathlib import Path

from company_os.synthetic_faq import answer, evaluate, load_json
from company_os.utils import canonical_json, sha256_file


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


def test_synthetic_customer_c_delivery_report_is_hash_bound() -> None:
    report = json.loads((EXAMPLE / "delivery_report.json").read_text(encoding="utf-8"))
    files = report["manifest"]["files"]
    manifest_core = {"files": files}

    assert report["manifest"]["sha256"] == hashlib.sha256(
        canonical_json(manifest_core).encode("utf-8")
    ).hexdigest()
    for name in ("customer_input.json", "faq_data.json", "eval_cases.json"):
        item = next(entry for entry in files if entry["path"] == name)
        assert item["sha256"] == sha256_file(EXAMPLE / name)

    signature = report.pop("signature_envelope")
    assert signature["format"] == "DSSE_COMPATIBLE_UNSIGNED"
    assert signature["signatures"] == []
    assert signature["payload_sha256"] == hashlib.sha256(
        canonical_json(report).encode("utf-8")
    ).hexdigest()
