from __future__ import annotations

from pathlib import Path

import pytest

from company_os.application import CompanyOS

from .helpers import build_venture


def test_model_external_ids_are_scoped_to_each_venture(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as company:
        ventures = [build_venture(company, f"same-model-ids-{index}")[2] for index in range(5)]

        rows = company.store.query_all(
            "SELECT id, venture_id, external_ref FROM assumptions ORDER BY venture_id"
        )
        assert len(rows) == 5
        assert len({row["id"] for row in rows}) == 5
        assert len({row["venture_id"] for row in rows}) == 5
        assert {row["external_ref"] for row in rows} == {"assumption-1"}
        assert {row["venture_id"] for row in rows} == {item.id for item in ventures}

        source_rows = company.store.query_all(
            """
            SELECT id, idea_id, external_ref, trusted FROM evidence
            WHERE kind = 'SYNTHETIC_FIXTURE' AND idea_id IS NOT NULL
            ORDER BY idea_id
            """
        )
        assert len(source_rows) == 5
        assert len({row["id"] for row in source_rows}) == 5
        assert {row["external_ref"] for row in source_rows} == {"source-evidence-1"}
        assert {row["trusted"] for row in source_rows} == {1}

        model_rows = company.store.query_all(
            """
            SELECT kind, trusted FROM evidence
            WHERE kind = 'MODEL_OUTPUT' AND idea_id IS NOT NULL
            """
        )
        assert len(model_rows) == 15
        assert {row["trusted"] for row in model_rows} == {0}

        venture_source_rows = company.store.query_all(
            """
            SELECT id, venture_id, external_ref, trusted FROM evidence
            WHERE kind = 'SYNTHETIC_FIXTURE' AND venture_id IS NOT NULL
            """
        )
        assert len(venture_source_rows) == 5
        assert len({row["id"] for row in venture_source_rows}) == 5
        assert {row["external_ref"] for row in venture_source_rows} == {
            "source-evidence-1"
        }
        assert {row["trusted"] for row in venture_source_rows} == {1}


def test_scaffold_database_failure_leaves_no_orphan_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with CompanyOS(tmp_path) as company:
        build_venture(company, "atomic-seed")
        third_idea = company.create_idea(
            "Synthetic atomic failure.", idempotency_key="atomic-failure-idea"
        )
        company.prepare_council(third_idea.id)
        from company_os.fakes import FakeCMO, FakeCPO, FakeCTO

        for fake in (FakeCTO(), FakeCPO(), FakeCMO()):
            response = fake.write_response(company.root, third_idea)
            company.ingest_council_response(
                third_idea.id, role=fake.role, response_file=response
            )
        contract = company.compile_council(
            third_idea.id, idempotency_key="atomic-failure-compile"
        )

        original_insert = company.store.insert_row

        def fail_on_metric(table, values, **kwargs):
            if table == "metrics":
                raise RuntimeError("synthetic injected DB failure")
            return original_insert(table, values, **kwargs)

        before = set((tmp_path / "var" / "ventures").glob("venture_*"))
        monkeypatch.setattr(company.store, "insert_row", fail_on_metric)
        with pytest.raises(RuntimeError, match="injected DB failure"):
            company.record_approval_and_scaffold(
                contract.contract_id,
                approval_status="NOT_REQUIRED",
                idempotency_key="atomic-failure-scaffold",
            )
        after = set((tmp_path / "var" / "ventures").glob("venture_*"))

        assert after == before
        staging = tmp_path / "var" / "ventures" / ".staging"
        assert not staging.exists() or list(staging.iterdir()) == []
