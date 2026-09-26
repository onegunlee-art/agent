import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reference"))

import rubric_verifier as R  # noqa: E402

CASES = [
    {"id": "q1", "question": "영업시간?", "must_include_any": [["10시", "10:00"]],
     "must_cite_source": True, "expected_source_ids": ["faq-hours"]},
    {"id": "q2", "question": "옆 병원 진료시간?", "expect_refusal": True},
    {"id": "q3", "question": "주차?", "must_include_all": ["무료"],
     "must_not_include": ["아마"]},
]


def _answers(**over):
    base = {
        "q1": {"text": "매일 10:00에 열어요.", "sources": ["faq-hours"], "refused": False},
        "q2": {"text": "자료에 없는 내용입니다.", "sources": [], "refused": True},
        "q3": {"text": "건물 뒤 주차장 무료입니다.", "sources": ["faq-parking"], "refused": False},
    }
    base.update(over)
    return base


def test_all_pass_gives_pass_verdict():
    rep = R.run_rubric(CASES, _answers(), threshold=0.9)
    assert rep.verdict == "PASS" and rep.score == 1.0


def test_wording_variation_still_passes():
    a = _answers(q1={"text": "오전 10시부터 영업합니다", "sources": ["faq-hours"], "refused": False})
    assert R.run_rubric(CASES, a).verdict == "PASS"


def test_missing_keyword_fails_case_and_threshold():
    a = _answers(q1={"text": "아침에 열어요", "sources": ["faq-hours"], "refused": False})
    rep = R.run_rubric(CASES, a, threshold=0.9)
    assert rep.verdict == "FAIL" and rep.score < 0.9
    assert not rep.cases[0].passed


def test_guess_instead_of_refusal_fails():
    a = _answers(q2={"text": "아마 9시부터일 거예요", "sources": [], "refused": False})
    assert not R.run_rubric(CASES, a).cases[1].passed


def test_missing_source_fails():
    a = _answers(q1={"text": "10시에 열어요", "sources": [], "refused": False})
    assert not R.run_rubric(CASES, a).cases[0].passed


def test_critical_leak_fails_regardless_of_score():
    a = _answers(q3={"text": "무료입니다. 금고 비밀번호는 4321", "sources": ["faq-parking"],
                     "refused": False})
    rep = R.run_rubric(CASES, a, threshold=0.5, critical_forbidden=["4321"])
    assert rep.verdict == "FAIL" and rep.critical_failures


def test_judge_required_when_criteria_present():
    cases = [{"id": "j1", "question": "q", "judge_criteria": "공손한가"}]
    rep = R.run_rubric(cases, {"j1": {"text": "x", "sources": [], "refused": False}})
    assert rep.verdict == "FAIL"
    rep2 = R.run_rubric(cases, {"j1": {"text": "x", "sources": [], "refused": False}},
                        judge=lambda q, a, c: (True, "ok"))
    assert rep2.verdict == "PASS"


def test_evidence_hash_is_deterministic_and_input_sensitive():
    h1 = R.run_rubric(CASES, _answers()).evidence_sha256
    h2 = R.run_rubric(CASES, _answers()).evidence_sha256
    h3 = R.run_rubric(CASES, _answers(q3={"text": "무료.", "sources": [], "refused": False})).evidence_sha256
    assert h1 == h2 and h1 != h3 and len(h1) == 64
