from __future__ import annotations

from company_os.application import CompanyOS
from company_os.fakes import FakeCMO, FakeCPO, FakeCTO


def build_venture(company: CompanyOS, label: str):
    idea = company.create_idea(
        f"Create a deterministic synthetic artifact for {label}.",
        idempotency_key=f"{label}-idea",
    )
    company.prepare_council(idea.id)
    for fake_role in (FakeCTO(), FakeCPO(), FakeCMO()):
        response = fake_role.write_response(company.root, idea)
        company.ingest_council_response(
            idea.id,
            role=fake_role.role,
            response_file=response,
        )
    compiled = company.compile_council(
        idea.id,
        idempotency_key=f"{label}-compile",
    )
    venture = company.record_approval_and_scaffold(
        compiled.contract_id,
        approval_status="NOT_REQUIRED",
        idempotency_key=f"{label}-scaffold",
    )
    return idea, compiled, venture, company.first_work_order(venture.id)
