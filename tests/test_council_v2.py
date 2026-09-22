from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.cli import main
from company_os.errors import ConflictError, ValidationError
from company_os.fakes import FakeCMO, FakeCPO, FakeCTO
from company_os.utils import atomic_write_json


def _ingest(
    company: CompanyOS,
    idea,
    payloads: dict[str, dict],
    directory: Path,
) -> None:
    for role, payload in payloads.items():
        path = directory / f"{role}.json"
        atomic_write_json(path, payload)
        company.ingest_council_response(
            idea.id,
            role=role,
            response_file=path,
        )


def _payloads(idea) -> dict[str, dict]:
    return {
        fake.role: fake.response(idea)
        for fake in (FakeCTO(), FakeCPO(), FakeCMO())
    }


def test_distinct_role_owned_contributions_are_merged(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic distinct council.", idempotency_key="i")
        company.prepare_council(idea.id)
        payloads = _payloads(idea)
        payloads["cto"]["contract_contribution"]["pass_condition"] = (
            "verified_artifact_count >= 1 and verifier_status == PASS"
        )
        payloads["cpo"]["contract_contribution"]["metric"]["name"] = (
            "cpo_verified_artifact_count"
        )
        payloads["cpo"]["contract_contribution"]["observable_problem"][
            "statement"
        ] = "The CPO observes that no verified artifact is available."
        payloads["cmo"]["contract_contribution"]["cheapest_valid_experiment"][
            "description"
        ] = "The CMO proposes one local synthetic validation run."
        _ingest(company, idea, payloads, tmp_path / "responses")

        outcome = company.compile_council(idea.id, idempotency_key="compile")
        contract = json.loads(
            company.store.get_row("contracts", outcome.contract_id)["payload_json"]
        )

        assert outcome.gate_result.passed is True
        assert contract["pass_condition"].endswith("verifier_status == PASS")
        assert contract["metric"]["name"] == "cpo_verified_artifact_count"
        assert contract["observable_problem"]["statement"].startswith("The CPO")
        assert contract["cheapest_valid_experiment"]["description"].startswith(
            "The CMO"
        )
        assert set(contract["executive_outputs"]) == {"cto", "cpo", "cmo"}


def test_council_response_resubmission_supersedes_prior_version(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic council resubmit.", idempotency_key="i")
        company.prepare_council(idea.id)
        payloads = _payloads(idea)
        _ingest(company, idea, payloads, tmp_path / "responses-v1")

        replacement = deepcopy(payloads["cpo"])
        replacement["contract_contribution"]["metric"]["name"] = "replacement_metric"
        replacement_path = tmp_path / "replacement-cpo.json"
        atomic_write_json(replacement_path, replacement)
        company.ingest_council_response(
            idea.id,
            role="cpo",
            response_file=replacement_path,
        )

        rows = company.store.query_all(
            "SELECT version, status FROM council_responses "
            "WHERE idea_id = ? AND role = 'cpo' ORDER BY version",
            (idea.id,),
        )
        assert [tuple(row) for row in rows] == [
            (1, "SUPERSEDED"),
            (2, "ACTIVE"),
        ]
        outcome = company.compile_council(idea.id, idempotency_key="compile-v2")
        contract = json.loads(
            company.store.get_row("contracts", outcome.contract_id)["payload_json"]
        )
        assert contract["metric"]["name"] == "replacement_metric"


def test_shared_field_conflict_can_be_resolved_by_ceo_contract(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic council resolve.", idempotency_key="i")
        company.prepare_council(idea.id)
        payloads = _payloads(idea)
        payloads["cmo"]["contract_contribution"]["decision_level"] = "FP_FULL"
        _ingest(company, idea, payloads, tmp_path / "responses")

        with pytest.raises(ConflictError, match="conflict"):
            company.compile_council(idea.id, idempotency_key="compile-conflict")

        resolved = deepcopy(payloads["cto"]["contract_contribution"])
        resolved_path = tmp_path / "resolved-contract.json"
        atomic_write_json(resolved_path, resolved)
        outcome = company.resolve_council(
            idea.id,
            contract_file=resolved_path,
            min_decision_level="FP_STANDARD",
            idempotency_key="resolve",
        )

        assert outcome.gate_result.passed is True
        assert "COUNCIL_CONFLICT_RESOLVED" in {
            event["event_type"] for event in company.events()
        }
        inbox_items = company.inbox()["file_items"]
        assert any(item["payload"].get("status") == "RESOLVED" for item in inbox_items)


def test_policy_minimum_rejects_model_declared_lower_level(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic minimum level.", idempotency_key="i")
        company.prepare_council(idea.id)
        payloads = _payloads(idea)
        for payload in payloads.values():
            payload["contract_contribution"]["decision_level"] = "FP_LITE"
        _ingest(company, idea, payloads, tmp_path / "responses")

        outcome = company.compile_council(
            idea.id,
            min_decision_level="FP_FULL",
            idempotency_key="compile-full-minimum",
        )

        assert outcome.gate_result.passed is False
        assert "DECISION_LEVEL_BELOW_MINIMUM" in {
            item.code for item in outcome.gate_result.violations
        }


def test_fp_full_approval_is_authorized_only_by_approval_row(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic FP_FULL authority.", idempotency_key="i")
        company.prepare_council(idea.id)
        payloads = _payloads(idea)
        for payload in payloads.values():
            payload["contract_contribution"]["decision_level"] = "FP_FULL"
        cto = payloads["cto"]["contract_contribution"]
        cto.update(
            {
                "rights_and_authority_basis": "CEO owns the synthetic fixture.",
                "worst_realistic_loss": "One disposable local fixture.",
                "reversibility_assessment": "Fully reversible local operation.",
                "specialist_review_status": "NOT_REQUIRED_SYNTHETIC",
                "evidence_pack": ["source-evidence-1"],
                "explicit_ceo_approval": True,
            }
        )
        _ingest(company, idea, payloads, tmp_path / "responses")
        outcome = company.compile_council(
            idea.id,
            min_decision_level="FP_FULL",
            idempotency_key="compile-full",
        )

        assert outcome.gate_result.passed is False
        assert {item.code for item in outcome.gate_result.violations} == {
            "FP_FULL_CEO_APPROVAL_REQUIRED"
        }
        with pytest.raises(ValidationError, match="explicit CEO approval"):
            company.record_approval_and_scaffold(
                outcome.contract_id,
                approval_status="NOT_REQUIRED",
                idempotency_key="scaffold-with-self-declaration",
            )

        venture = company.record_approval_and_scaffold(
            outcome.contract_id,
            approval_status="APPROVED",
            idempotency_key="scaffold-with-db-approval",
        )
        approval = company.store.query_one(
            "SELECT * FROM approvals WHERE contract_id = ?",
            (outcome.contract_id,),
        )
        assert venture.status == "ACTIVE"
        assert approval is not None
        assert approval["status"] == "APPROVED"
        assert approval["actor"] == "CEO"


def test_cli_council_resolve_accepts_complete_ceo_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic CLI resolve.", idempotency_key="i")
        company.prepare_council(idea.id)
        payloads = _payloads(idea)
        payloads["cmo"]["contract_contribution"]["decision_level"] = "FP_FULL"
        _ingest(company, idea, payloads, tmp_path / "responses")
        resolved_path = tmp_path / "ceo-contract.json"
        atomic_write_json(
            resolved_path,
            deepcopy(payloads["cto"]["contract_contribution"]),
        )

    assert (
        main(
            (
                "--root",
                str(tmp_path),
                "council",
                "resolve",
                idea.id,
                "--contract-file",
                str(resolved_path),
            )
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["gate"]["passed"] is True
    assert output["contract_id"].startswith("contract_")
