from __future__ import annotations

import json
from pathlib import Path

from company_os.application import CompanyOS
from company_os.fakes import FakeExecutor

from .helpers import build_venture


def test_verifier_hash_change_invalidates_run_and_records_event(tmp_path: Path) -> None:
    company = CompanyOS(root=tmp_path)
    company.initialize()
    _, _, _, work_order = build_venture(company, "synthetic-tamper")

    verifier = json.loads(work_order.verifier_path.read_text(encoding="utf-8"))
    verifier["expected_content"] = "tampered expectation\n"
    work_order.verifier_path.write_text(
        json.dumps(verifier, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    run = company.execute_work_order(
        work_order.id,
        executor=FakeExecutor(),
        idempotency_key="tampered-run",
    )

    assert run.status == "INVALIDATED"
    assert "VERIFIER_TAMPER_DETECTED" in {
        row["event_type"] for row in company.events()
    }
    evidence = company.evidence_for_run(run.id)
    assert evidence
    assert all(item.trusted is False for item in evidence)
