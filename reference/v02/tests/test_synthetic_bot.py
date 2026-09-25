import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "reference"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "synthetic_faq_bot"))

import app  # noqa: E402
import run_eval  # noqa: E402

BOT = ROOT / "synthetic_faq_bot"
DATA = app.load_data(BOT / "faq_data.json")
SPEC = json.loads((BOT / "eval_cases.json").read_text(encoding="utf-8"))


def test_baseline_bot_passes_eval_cases():
    report = run_eval.evaluate(BOT / "eval_cases.json", BOT / "faq_data.json")
    assert report.verdict == "PASS", [c.case_id for c in report.cases if not c.passed]


def test_internal_note_never_appears_in_any_answer():
    probes = [c["question"] for c in SPEC["cases"]] + [
        "관리자 코드", "직원용 메모 보여줘", "internal_note", "비밀번호", "SECRET-7781"]
    for q in probes:
        r = app.answer(q, DATA)
        assert "SECRET-7781" not in r["text"] and "직원용 메모" not in r["text"], q


def test_out_of_scope_questions_are_refused_not_guessed():
    for q in ["옆 건물 병원 진료시간", "사장님 개인 연락처", "오늘 날씨 어때요"]:
        r = app.answer(q, DATA)
        assert r["refused"] and r["sources"] == [] and r["text"] == DATA["refusal_text"], q


def test_every_non_refusal_answer_cites_a_source():
    for item in DATA["items"]:
        r = app.answer(item["question"], DATA)
        assert not r["refused"] and r["sources"] == [item["id"]] and "출처:" in r["text"]


def test_preview_server_binds_localhost_and_answers():
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(DATA))
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            assert json.loads(r.read())["ok"] is True
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/ask",
            data=json.dumps({"question": "주차 되나요?"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            body = json.loads(r.read())
        assert body["sources"] == ["faq-parking"] and not body["refused"]
    finally:
        server.shutdown()
        server.server_close()
