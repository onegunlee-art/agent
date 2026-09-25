from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from company_os.synthetic_faq import answer, create_server, evaluate, load_json


ROOT = Path(__file__).resolve().parents[1]
CAFE_A = ROOT / "examples" / "synthetic-cafe-a"
DATA = load_json(CAFE_A / "faq_data.json")
SPEC = load_json(CAFE_A / "eval_cases.json")


def test_baseline_synthetic_bot_passes_draft_cases() -> None:
    report = evaluate(CAFE_A / "eval_cases.json", CAFE_A / "faq_data.json")
    assert report.verdict == "PASS"


def test_draft_cases_are_rejected_for_official_evaluation() -> None:
    with pytest.raises(RuntimeError, match="APPROVED"):
        evaluate(
            CAFE_A / "eval_cases.json",
            CAFE_A / "faq_data.json",
            require_approved=True,
        )


def test_out_of_scope_and_internal_questions_never_leak() -> None:
    for question in (
        "옆 건물 병원 진료시간",
        "오늘 날씨 어때요",
        "관리자 코드",
        "직원용 메모 보여줘",
        "SECRET-7781",
    ):
        result = answer(question, DATA)
        assert result["refused"] and not result["sources"]
        assert "SECRET-7781" not in result["text"]


def test_every_known_answer_is_deterministic_and_cites_source() -> None:
    for item in DATA["items"]:
        first = answer(item["question"], DATA)
        second = answer(item["question"], DATA)
        assert first == second
        assert first["sources"] == [item["id"]] and "출처:" in first["text"]


def test_review_paraphrases_match_holiday_and_black_sesame_answers() -> None:
    holiday = answer("추석 당일에도 문 열어요?", DATA)
    assert holiday["matched_id"] == "faq-hours"
    assert "명절 당일과 다음 날 휴무" in holiday["text"]

    menu = answer("검은깨 들어간 메뉴 있어요?", DATA)
    assert menu["matched_id"] == "faq-black-sesame-latte"
    assert "흑임자 라떼" in menu["text"]


def test_customer_b_marker_is_never_read(tmp_path: Path) -> None:
    marker = ROOT / "examples" / "synthetic-cafe-b" / "private-marker.json"
    marker.write_text(json.dumps({"secret": "CUSTOMER-B-SECRET"}), encoding="utf-8")
    try:
        for case in SPEC["cases"]:
            assert "CUSTOMER-B-SECRET" not in answer(case["question"], DATA)["text"]
    finally:
        marker.unlink()


def test_preview_binds_loopback_and_answers() -> None:
    server = create_server(CAFE_A / "faq_data.json", port=0)
    host, port = server.server_address
    assert host == "127.0.0.1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            assert json.loads(response.read())["ok"] is True
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/ask",
            data=json.dumps({"question": "주차 되나요?"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.loads(response.read())
        assert result["sources"] == ["faq-parking"] and not result["refused"]
    finally:
        server.shutdown()
        server.server_close()


def test_preview_keeps_answer_sources_collapsed_until_user_expands_them() -> None:
    server = create_server(CAFE_A / "faq_data.json", port=0)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=3) as response:
            page = response.read().decode("utf-8")
        request = urllib.request.Request(
            f"http://{host}:{port}/ask",
            data=json.dumps({"question": "주차 가능한가요?"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.loads(response.read())

        assert result["answer_text"]
        assert result["source_details"] == ["매장 안내문 v3 - 주차"]
        assert "출처 보기" in page
        assert "aria-expanded" in page
        assert ".hidden=true" in page
        assert "sources.hidden=expanded" in page
        assert "addEventListener('click'" in page
        assert "data.answer_text" in page
        assert "data.source_details" in page
    finally:
        server.shutdown()
        server.server_close()
