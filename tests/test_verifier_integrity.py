from __future__ import annotations

import json
from pathlib import Path

from company_os.application import CompanyOS
from company_os.fakes import FakeExecutor
from company_os.utils import payload_hash
from company_os.verifier import write_exact_text_verifier

from .helpers import build_venture


def test_synthetic_verifier_hash_binds_declared_contract_context(
    tmp_path: Path,
) -> None:
    first_binding = {
        "metric": {"name": "first"},
        "pass_condition": "first condition",
    }
    second_binding = {
        "metric": {"name": "second"},
        "pass_condition": "second condition",
    }
    first_path = tmp_path / "first-verifier.json"
    second_path = tmp_path / "second-verifier.json"

    first_hash = write_exact_text_verifier(
        first_path,
        artifact_relative_path="artifacts/result.txt",
        expected_content="same fixture bytes\n",
        contract_binding=first_binding,
    )
    second_hash = write_exact_text_verifier(
        second_path,
        artifact_relative_path="artifacts/result.txt",
        expected_content="same fixture bytes\n",
        contract_binding=second_binding,
    )

    first_spec = json.loads(first_path.read_text(encoding="utf-8"))
    assert first_hash != second_hash
    assert first_spec["verification_scope"] == "SYNTHETIC_ONLY"
    assert first_spec["verifier_semantics"] == "EXACT_TEXT_FIXTURE_ONLY"
    assert first_spec["contract_binding_sha256"] == payload_hash(first_binding)


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
