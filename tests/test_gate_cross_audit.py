from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.errors import ValidationError
from company_os.fakes import FakeCMO, FakeCPO, FakeCTO
from company_os.models import Idea
from company_os.utils import atomic_write_json, payload_hash


def _prepare_council(
    company: CompanyOS,
    directory: Path,
    *,
    label: str,
) -> tuple[Idea, dict[str, dict]]:
    idea = company.create_idea(
        f"Synthetic cross-audit fixture for {label}.",
        idempotency_key=f"{label}-idea",
    )
    company.prepare_council(idea.id)
    payloads = {
        fake.role: fake.response(idea)
        for fake in (FakeCTO(), FakeCPO(), FakeCMO())
    }
    for role, payload in payloads.items():
        response_path = directory / label / f"{role}.json"
        atomic_write_json(response_path, payload)
        company.ingest_council_response(
            idea.id,
            role=role,
            response_file=response_path,
        )
    return idea, payloads


def test_scaffold_promotion_failure_can_resume_with_same_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with CompanyOS(tmp_path) as company:
        idea, _ = _prepare_council(company, tmp_path, label="promotion-retry")
        compiled = company.compile_council(
            idea.id,
            idempotency_key="promotion-retry-compile",
        )

        original_replace = Path.replace
        failed_once = False

        def fail_first_staging_promotion(
            source: Path,
            target: str | Path,
        ) -> Path:
            nonlocal failed_once
            if not failed_once and ".staging" in source.parts:
                failed_once = True
                raise PermissionError("synthetic one-shot promotion failure")
            return original_replace(source, target)

        monkeypatch.setattr(Path, "replace", fail_first_staging_promotion)
        scaffold_key = "promotion-retry-scaffold"
        with pytest.raises(ValidationError, match="promoted from staging"):
            company.record_approval_and_scaffold(
                compiled.contract_id,
                approval_status="NOT_REQUIRED",
                idempotency_key=scaffold_key,
            )

        venture = company.record_approval_and_scaffold(
            compiled.contract_id,
            approval_status="NOT_REQUIRED",
            idempotency_key=scaffold_key,
        )

        venture_rows = company.store.query_all("SELECT * FROM ventures")
        workspace_directories = list(
            (tmp_path / "var" / "ventures").glob("venture_*")
        )
        staging_root = tmp_path / "var" / "ventures" / ".staging"
        assert failed_once is True
        assert venture.status == "ACTIVE"
        assert venture.workspace_path.is_dir()
        assert venture.context_manifest_path.is_file()
        assert len(venture_rows) == len(workspace_directories) == 1
        assert not staging_root.exists() or not any(staging_root.iterdir())


def test_scaffold_persists_separate_facts_and_assumptions(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        idea, payloads = _prepare_council(
            company,
            tmp_path,
            label="separate-claims",
        )
        resolved_contract = deepcopy(payloads["cto"]["contract_contribution"])
        claims = resolved_contract.pop("claims")
        resolved_contract["facts"] = [
            claim for claim in claims if claim["type"] == "FACT"
        ]
        resolved_contract["assumptions"] = [
            claim for claim in claims if claim["type"] == "ASSUMPTION"
        ]
        contract_path = tmp_path / "separate-claims-contract.json"
        atomic_write_json(contract_path, resolved_contract)

        compiled = company.resolve_council(
            idea.id,
            contract_file=contract_path,
            idempotency_key="separate-claims-resolve",
        )
        assert compiled.gate_result.passed is True
        venture = company.record_approval_and_scaffold(
            compiled.contract_id,
            approval_status="NOT_REQUIRED",
            idempotency_key="separate-claims-scaffold",
        )

        assumption_rows = company.store.query_all(
            "SELECT id, external_ref FROM assumptions WHERE venture_id = ?",
            (venture.id,),
        )
        expected_refs = {
            claim["id"] for claim in resolved_contract["assumptions"]
        }
        experiment = company.store.query_one(
            "SELECT assumption_id FROM experiments WHERE venture_id = ?",
            (venture.id,),
        )
        assert {row["external_ref"] for row in assumption_rows} == expected_refs
        assert all(row["id"] != row["external_ref"] for row in assumption_rows)
        assert experiment is not None
        assert experiment["assumption_id"] in {
            row["id"] for row in assumption_rows
        }


def test_resubmitting_superseded_response_reactivates_that_payload(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea(
            "Synthetic A-B-A council response fixture.",
            idempotency_key="aba-idea",
        )
        company.prepare_council(idea.id)
        response_a = FakeCPO().response(idea)
        response_b = deepcopy(response_a)
        response_b["contract_contribution"]["metric"]["name"] = "metric_b"
        response_a_path = tmp_path / "response-a.json"
        response_b_path = tmp_path / "response-b.json"
        atomic_write_json(response_a_path, response_a)
        atomic_write_json(response_b_path, response_b)

        company.ingest_council_response(
            idea.id,
            role="cpo",
            response_file=response_a_path,
        )
        company.ingest_council_response(
            idea.id,
            role="cpo",
            response_file=response_b_path,
        )
        company.ingest_council_response(
            idea.id,
            role="cpo",
            response_file=response_a_path,
        )

        rows = company.store.query_all(
            "SELECT version, status, response_hash, payload_json "
            "FROM council_responses WHERE idea_id = ? AND role = 'cpo' "
            "ORDER BY version",
            (idea.id,),
        )
        active_rows = [row for row in rows if row["status"] == "ACTIVE"]
        assert len(active_rows) == 1
        assert active_rows[0]["response_hash"] == payload_hash(response_a)
        assert json.loads(active_rows[0]["payload_json"]) == response_a
        assert all(
            row["status"] == "SUPERSEDED"
            for row in rows
            if row["response_hash"] != payload_hash(response_a)
        )
