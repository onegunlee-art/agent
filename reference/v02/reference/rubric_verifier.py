"""rubric_verifier.py — AI 결과물용 항목별 채점 검증기 (WorkOrder C).

exact-text 비교는 합성 fixture 전용으로 남기고, 실제 모델 결과물은 이 검증기로 판정한다.

평가 사례(case) 형식 (JSON):
{
  "id": "q01",
  "question": "영업시간이 어떻게 되나요?",
  "must_include_any": [["10시", "10:00"], ["21시", "21:00"]],   # 그룹마다 하나 이상 포함
  "must_include_all": ["매일"],                                 # 전부 포함
  "must_not_include": ["아마", "추측"],                          # 포함되면 실패
  "expect_refusal": false,                                      # 거절 여부가 일치해야 함
  "must_cite_source": true,                                     # sources 비어 있으면 실패
  "expected_source_ids": ["faq-hours"],                         # 있으면 교집합 필요
  "judge_criteria": "...",                                      # LLM 판정 기준(선택)
  "weight": 1
}

답변(answer) 형식: {"text": str, "sources": [str], "refused": bool}

Report.verdict 는 (a) critical 위반 0건 이고 (b) 가중 점수 >= threshold 일 때만 PASS.
critical_forbidden(비밀정보·다른 고객 표식 등)이 하나라도 나오면 점수와 무관하게 FAIL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional, Sequence

Judge = Callable[[str, str, str], tuple[bool, str]]  # (question, answer_text, criteria) -> (ok, reason)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    weight: float
    checks: list[Check] = field(default_factory=list)
    critical_failures: list[str] = field(default_factory=list)


@dataclass
class Report:
    verdict: str
    score: float
    threshold: float
    total_weight: float
    earned_weight: float
    critical_failures: list[str]
    cases: list[CaseResult]
    evidence_sha256: str


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _contains(haystack: str, needle: str) -> bool:
    return normalize(needle) in haystack


def evaluate_case(
    case: dict,
    answer: Optional[dict],
    critical_forbidden: Sequence[str] = (),
    judge: Optional[Judge] = None,
) -> CaseResult:
    weight = float(case.get("weight", 1))
    result = CaseResult(case_id=case["id"], passed=False, weight=weight)

    if answer is None:
        result.checks.append(Check("answer_present", False, "답변 없음"))
        return result

    text = normalize(answer.get("text", ""))
    sources = list(answer.get("sources") or [])
    refused = bool(answer.get("refused", False))
    checks = result.checks

    # 1. critical: 비밀정보/다른 고객 자료 혼입 — 점수와 무관하게 전체 FAIL
    for token in critical_forbidden:
        if _contains(text, token):
            result.critical_failures.append(f"{case['id']}: critical token '{token}' 노출")
            checks.append(Check("critical_forbidden", False, token))

    # 2. 거절 여부
    if "expect_refusal" in case:
        want = bool(case["expect_refusal"])
        checks.append(Check("refusal", refused == want,
                            f"expected={want} actual={refused}"))

    # 거절이 정답인 사례는 내용 검사를 생략한다 (거절문 자체가 정답)
    content_checks = not case.get("expect_refusal", False)

    if content_checks:
        for group in case.get("must_include_any", []):
            ok = any(_contains(text, alt) for alt in group)
            checks.append(Check("must_include_any", ok, " | ".join(group)))
        for needle in case.get("must_include_all", []):
            checks.append(Check("must_include_all", _contains(text, needle), needle))
        if case.get("must_cite_source"):
            ok = bool(sources)
            expected = case.get("expected_source_ids")
            if ok and expected:
                ok = bool(set(sources) & set(expected))
            checks.append(Check("must_cite_source", ok, f"sources={sources}"))
        if case.get("judge_criteria"):
            if judge is None:
                checks.append(Check("judge", False, "judge_criteria가 있으나 판정자가 없음"))
            else:
                ok, reason = judge(case["question"], answer.get("text", ""), case["judge_criteria"])
                checks.append(Check("judge", bool(ok), reason))

    for needle in case.get("must_not_include", []):
        checks.append(Check("must_not_include", not _contains(text, needle), needle))

    result.passed = all(c.passed for c in checks) and not result.critical_failures
    return result


def run_rubric(
    cases: Sequence[dict],
    answers: dict[str, dict],
    threshold: float = 0.9,
    critical_forbidden: Sequence[str] = (),
    judge: Optional[Judge] = None,
) -> Report:
    results = [evaluate_case(c, answers.get(c["id"]), critical_forbidden, judge) for c in cases]
    total = sum(r.weight for r in results) or 1.0
    earned = sum(r.weight for r in results if r.passed)
    critical = [f for r in results for f in r.critical_failures]
    score = round(earned / total, 4)
    verdict = "PASS" if (not critical and score >= threshold) else "FAIL"

    canonical = json.dumps(
        {"cases": list(cases), "answers": answers, "threshold": threshold,
         "critical_forbidden": list(critical_forbidden),
         "results": [asdict(r) for r in results]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Report(verdict, score, threshold, total, earned, critical, results, digest)


def report_to_dict(report: Report) -> dict:
    return asdict(report)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="항목별 채점 검증기")
    p.add_argument("cases", help="평가 사례 JSON (배열)")
    p.add_argument("answers", help="답변 JSON ({case_id: answer})")
    p.add_argument("--threshold", type=float, default=0.9)
    p.add_argument("--critical", nargs="*", default=[], help="노출 금지 문자열")
    p.add_argument("--out", help="리포트 JSON 저장 경로 (Evidence용)")
    a = p.parse_args(argv)
    with open(a.cases, encoding="utf-8") as f:
        cases = json.load(f)
    with open(a.answers, encoding="utf-8") as f:
        answers = json.load(f)
    report = run_rubric(cases, answers, a.threshold, a.critical)
    payload = json.dumps(report_to_dict(report), ensure_ascii=False, indent=2)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(payload)
    print(payload)
    return 0 if report.verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
