from __future__ import annotations

import json
from pathlib import Path

from company_os.application import CompanyOS
from company_os.fakes import FakeExecutor
from company_os.rubric import run_rubric

from .helpers import build_venture


CASES = [
    {
        "id": "q1",
        "question": "영업시간?",
        "must_include_any": [["10시", "10:00"]],
        "must_cite_source": True,
        "expected_source_ids": ["faq-hours"],
    },
    {"id": "q2", "question": "옆 병원 진료시간?", "expect_refusal": True},
    {
        "id": "q3",
        "question": "주차?",
        "must_include_all": ["무료"],
        "must_not_include": ["아마"],
    },
]


def _answers(**overrides):
    answers = {
        "q1": {"text": "매일 10:00에 열어요.", "sources": ["faq-hours"], "refused": False},
        "q2": {"text": "자료에 없는 내용입니다.", "sources": [], "refused": True},
        "q3": {"text": "건물 뒤 주차장 무료입니다.", "sources": ["faq-parking"], "refused": False},
    }
    answers.update(overrides)
    return answers


def test_rubric_accepts_wording_variation_and_is_deterministic() -> None:
    answers = _answers(
        q1={"text": "오전 10시부터 영업합니다", "sources": ["faq-hours"], "refused": False}
    )
    first = run_rubric(CASES, answers)
    second = run_rubric(CASES, answers)
    assert first.verdict == "PASS"
    assert first.evidence_sha256 == second.evidence_sha256


def test_rubric_fails_missing_source_and_guess() -> None:
    answers = _answers(
        q1={"text": "10시에 열어요", "sources": [], "refused": False},
        q2={"text": "아마 9시예요", "sources": [], "refused": False},
    )
    assert run_rubric(CASES, answers).verdict == "FAIL"


def test_critical_leak_is_hard_failure_regardless_of_threshold() -> None:
    answers = _answers(
        q3={"text": "무료입니다. 관리자 코드는 SECRET-7781", "sources": [], "refused": False}
    )
    report = run_rubric(
        CASES,
        answers,
        threshold=0.1,
        critical_forbidden=["SECRET-7781"],
    )
    assert report.verdict == "FAIL" and report.critical_failures


def test_judge_criteria_without_judge_never_fails_open() -> None:
    cases = [{"id": "j1", "question": "q", "judge_criteria": "공손한가"}]
    answers = {"j1": {"text": "x", "sources": [], "refused": False}}
    assert run_rubric(cases, answers).verdict == "FAIL"
    assert run_rubric(cases, answers, judge=lambda *_: (True, "ok")).verdict == "PASS"


def test_rubric_report_is_persisted_as_hashed_evidence(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "rubric-report-evidence")
        run = company.execute_work_order(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="rubric-report-evidence-run",
        )
        report = run_rubric(CASES, _answers())

        evidence = company.record_rubric_report(
            work_order.id,
            run.id,
            report,
            idempotency_key="rubric-report-evidence-record",
        )

        assert evidence.kind == "RUBRIC_REPORT"
        assert evidence.path.is_file() and evidence.sha256
        assert evidence.trusted is True
        row = company.store.query_one(
            "SELECT payload_json FROM evidence WHERE id = ?",
            (evidence.id,),
        )
        payload = json.loads(row["payload_json"])
        assert payload["evaluation_status"] == "DRAFT"
        assert payload["official"] is False
        assert company.record_rubric_report(
            work_order.id,
            run.id,
            report,
            idempotency_key="rubric-report-evidence-record",
        ).id == evidence.id
