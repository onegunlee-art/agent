from __future__ import annotations

import json
from pathlib import Path

from company_os.application import CompanyOS
from company_os.fakes import FakeCMO, FakeCPO, FakeCTO, FakeExecutor


def test_idea_to_waiting_for_opus_survives_restart(tmp_path: Path) -> None:
    company = CompanyOS(root=tmp_path)
    company.initialize()

    idea = company.create_idea(
        "Create one deterministic synthetic artifact and verify its content.",
        idempotency_key="acceptance-idea-1",
    )
    requests = company.prepare_council(idea.id)

    assert {item.role for item in requests} == {"cto", "cpo", "cmo"}
    for item in requests:
        assert item.json_path.exists()
        assert item.markdown_path.exists()

    for fake_role in (FakeCTO(), FakeCPO(), FakeCMO()):
        response_path = fake_role.write_response(company.root, idea)
        company.ingest_council_response(
            idea.id,
            role=fake_role.role,
            response_file=response_path,
        )

    compiled = company.compile_council(
        idea.id,
        idempotency_key="acceptance-compile-1",
    )
    assert compiled.gate_result.passed is True

    venture = company.record_approval_and_scaffold(
        compiled.contract_id,
        approval_status="NOT_REQUIRED",
        idempotency_key="acceptance-scaffold-1",
    )
    assert venture.workspace_path.is_dir()
    assert venture.context_manifest_path.is_file()
    approval = company.store.query_one(
        "SELECT * FROM approvals WHERE contract_id = ?", (compiled.contract_id,)
    )
    assert approval is not None
    assert approval["status"] == "NOT_REQUIRED"
    assert "APPROVAL_STATUS_RECORDED" in {
        event["event_type"] for event in company.events()
    }

    manifest = json.loads(venture.context_manifest_path.read_text(encoding="utf-8"))
    assert manifest["venture_id"] == venture.id
    assert manifest["idea_id"] == idea.id

    work_order = company.first_work_order(venture.id)
    run = company.execute_work_order(
        work_order.id,
        executor=FakeExecutor(),
        idempotency_key="acceptance-run-1",
    )
    assert run.status == "PASS"
    assert company.evidence_for_run(run.id)

    review = company.prepare_review(
        work_order.id,
        idempotency_key="acceptance-review-1",
    )
    assert review.status == "WAITING_FOR_OPUS"
    assert review.json_path.is_file()
    assert review.markdown_path.is_file()
    assert company.work_order(work_order.id).status == "WAITING_FOR_OPUS"

    company.close()

    restarted = CompanyOS(root=tmp_path)
    restarted.initialize()

    restored_work_order = restarted.work_order(work_order.id)
    restored_review = restarted.review(review.id)
    restored_runs = restarted.runs_for_work_order(work_order.id)

    assert restored_work_order.status == "WAITING_FOR_OPUS"
    assert restored_review.status == "WAITING_FOR_OPUS"
    assert restored_review.json_path == review.json_path
    assert [item.id for item in restored_runs] == [run.id]
