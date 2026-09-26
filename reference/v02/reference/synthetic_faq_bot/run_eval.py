"""합성 챗봇 평가 실행기. 종료 코드 0=PASS, 1=FAIL.

사용: python run_eval.py [--cases eval_cases.json] [--data faq_data.json] [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))          # reference/  → rubric_verifier
sys.path.insert(0, str(HERE))                 # synthetic_faq_bot/ → app

import rubric_verifier as R  # noqa: E402
import app  # noqa: E402


def evaluate(cases_path: Path, data_path: Path, allow_draft: bool = True) -> R.Report:
    spec = json.loads(cases_path.read_text(encoding="utf-8"))
    if spec.get("_status") != "APPROVED" and not allow_draft:
        raise RuntimeError("평가 사례가 CEO 확정(APPROVED) 상태가 아닙니다.")
    data = app.load_data(data_path)
    answers = {c["id"]: app.answer(c["question"], data) for c in spec["cases"]}
    return R.run_rubric(spec["cases"], answers, spec.get("threshold", 0.9),
                        spec.get("critical_forbidden", []))


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cases", default=str(HERE / "eval_cases.json"))
    p.add_argument("--data", default=str(HERE / "faq_data.json"))
    p.add_argument("--out", help="리포트 JSON 저장 경로")
    p.add_argument("--require-approved", action="store_true",
                   help="공식 평가: 사례가 APPROVED가 아니면 실패")
    a = p.parse_args(argv)
    report = evaluate(Path(a.cases), Path(a.data), allow_draft=not a.require_approved)
    payload = json.dumps(R.report_to_dict(report), ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(payload, encoding="utf-8")
    for c in report.cases:
        mark = "OK " if c.passed else "NG "
        failed = [f"{k.name}({k.detail})" for k in c.checks if not k.passed]
        print(mark, c.case_id, "" if c.passed else failed)
    print(f"verdict={report.verdict} score={report.score} threshold={report.threshold} "
          f"evidence_sha256={report.evidence_sha256[:12]}…")
    return 0 if report.verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
