from __future__ import annotations

import json
from pathlib import Path

from company_os.application import CompanyOS
from company_os.fakes import FakeExecutor

from .helpers import build_venture


def test_unrelated_ventures_are_isolated(tmp_path: Path) -> None:
    company = CompanyOS(root=tmp_path)
    company.initialize()
    idea_a, _, venture_a, work_a = build_venture(company, "synthetic-alpha")
    idea_b, _, venture_b, work_b = build_venture(company, "synthetic-beta")

    run_a = company.execute_work_order(
        work_a.id,
        executor=FakeExecutor(),
        idempotency_key="alpha-run",
    )
    run_b = company.execute_work_order(
        work_b.id,
        executor=FakeExecutor(),
        idempotency_key="beta-run",
    )

    assert venture_a.workspace_path != venture_b.workspace_path
    assert venture_a.context_manifest_path != venture_b.context_manifest_path
    assert venture_a.workspace_path not in venture_b.workspace_path.parents
    assert venture_b.workspace_path not in venture_a.workspace_path.parents

    manifest_a = venture_a.context_manifest_path.read_text(encoding="utf-8")
    manifest_b = venture_b.context_manifest_path.read_text(encoding="utf-8")
    assert venture_b.id not in manifest_a
    assert idea_b.id not in manifest_a
    assert venture_a.id not in manifest_b
    assert idea_a.id not in manifest_b

    assert company.assumptions_for_venture(venture_a.id)
    assert company.assumptions_for_venture(venture_b.id)
    assert all(
        row["venture_id"] == venture_a.id
        for row in company.assumptions_for_venture(venture_a.id)
    )
    assert all(
        row["venture_id"] == venture_b.id
        for row in company.assumptions_for_venture(venture_b.id)
    )
    assert all(
        item.venture_id == venture_a.id for item in company.evidence_for_run(run_a.id)
    )
    assert all(
        item.venture_id == venture_b.id for item in company.evidence_for_run(run_b.id)
    )
    assert all(
        row["venture_id"] == venture_a.id
        for row in company.decisions_for_venture(venture_a.id)
    )
    assert all(
        row["venture_id"] == venture_b.id
        for row in company.decisions_for_venture(venture_b.id)
    )


def test_venture_workspace_rejects_path_traversal(tmp_path: Path) -> None:
    company = CompanyOS(root=tmp_path)
    company.initialize()

    try:
        company.venture_workspace("../outside")
    except ValueError as exc:
        assert "invalid Venture id" in str(exc)
    else:
        raise AssertionError("path traversal was accepted")
