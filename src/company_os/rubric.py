"""Deterministic rubric evaluation for variable AI-produced text."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Sequence

Judge = Callable[[str, str, str], tuple[bool, str]]


@dataclass(frozen=True)
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
class RubricReport:
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
    answer: dict | None,
    critical_forbidden: Sequence[str] = (),
    judge: Judge | None = None,
) -> CaseResult:
    weight = float(case.get("weight", 1))
    result = CaseResult(case_id=str(case["id"]), passed=False, weight=weight)
    if answer is None:
        result.checks.append(Check("answer_present", False, "answer missing"))
        return result

    text = normalize(str(answer.get("text", "")))
    sources = list(answer.get("sources") or [])
    refused = bool(answer.get("refused", False))
    for token in critical_forbidden:
        if _contains(text, token):
            result.critical_failures.append(
                f"{case['id']}: critical token {token!r} exposed"
            )
            result.checks.append(Check("critical_forbidden", False, token))

    if "expect_refusal" in case:
        expected = bool(case["expect_refusal"])
        result.checks.append(
            Check("refusal", refused == expected, f"expected={expected} actual={refused}")
        )

    if not case.get("expect_refusal", False):
        for group in case.get("must_include_any", []):
            alternatives = [str(item) for item in group]
            result.checks.append(
                Check(
                    "must_include_any",
                    any(_contains(text, item) for item in alternatives),
                    " | ".join(alternatives),
                )
            )
        for item in case.get("must_include_all", []):
            needle = str(item)
            result.checks.append(
                Check("must_include_all", _contains(text, needle), needle)
            )
        if case.get("must_cite_source"):
            cited = bool(sources)
            expected_sources = case.get("expected_source_ids")
            if cited and expected_sources:
                cited = bool(set(sources) & set(expected_sources))
            result.checks.append(
                Check("must_cite_source", cited, f"sources={sources}")
            )
        criteria = case.get("judge_criteria")
        if criteria:
            if judge is None:
                result.checks.append(
                    Check("judge", False, "judge criteria present but no judge supplied")
                )
            else:
                passed, reason = judge(
                    str(case.get("question", "")),
                    str(answer.get("text", "")),
                    str(criteria),
                )
                result.checks.append(Check("judge", bool(passed), reason))

    for item in case.get("must_not_include", []):
        needle = str(item)
        result.checks.append(
            Check("must_not_include", not _contains(text, needle), needle)
        )

    result.passed = (
        bool(result.checks)
        and all(check.passed for check in result.checks)
        and not result.critical_failures
    )
    return result


def run_rubric(
    cases: Sequence[dict],
    answers: dict[str, dict],
    threshold: float = 0.9,
    critical_forbidden: Sequence[str] = (),
    judge: Judge | None = None,
) -> RubricReport:
    if not 0 <= threshold <= 1:
        raise ValueError("rubric threshold must be between 0 and 1")
    results = [
        evaluate_case(case, answers.get(str(case["id"])), critical_forbidden, judge)
        for case in cases
    ]
    total = sum(item.weight for item in results) or 1.0
    earned = sum(item.weight for item in results if item.passed)
    critical = [failure for item in results for failure in item.critical_failures]
    score = round(earned / total, 4)
    verdict = "PASS" if not critical and score >= threshold else "FAIL"
    canonical = json.dumps(
        {
            "cases": list(cases),
            "answers": answers,
            "threshold": threshold,
            "critical_forbidden": list(critical_forbidden),
            "results": [asdict(item) for item in results],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return RubricReport(
        verdict=verdict,
        score=score,
        threshold=threshold,
        total_weight=total,
        earned_weight=earned,
        critical_failures=critical,
        cases=results,
        evidence_sha256=digest,
    )


def report_as_dict(report: RubricReport) -> dict:
    return asdict(report)
