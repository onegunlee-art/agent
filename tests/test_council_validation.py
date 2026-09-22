from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.errors import ConflictError, ValidationError
from company_os.fakes import FakeCMO, FakeCPO, FakeCTO
from company_os.utils import atomic_write_json


def test_council_requires_all_three_responses(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic incomplete council.", idempotency_key="i")
        company.prepare_council(idea.id)
        response = FakeCTO().write_response(company.root, idea)
        company.ingest_council_response(idea.id, role="cto", response_file=response)

        with pytest.raises(ValidationError, match="missing"):
            company.compile_council(idea.id, idempotency_key="compile")


def test_gate_failure_prevents_venture_workspace(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic invalid contract.", idempotency_key="i")
        company.prepare_council(idea.id)
        for fake in (FakeCTO(), FakeCPO(), FakeCMO()):
            payload = fake.response(idea)
            payload["contract_contribution"].pop("metric")
            response = tmp_path / f"{fake.role}.json"
            atomic_write_json(response, payload)
            company.ingest_council_response(
                idea.id, role=fake.role, response_file=response
            )

        outcome = company.compile_council(idea.id, idempotency_key="compile")
        assert outcome.gate_result.passed is False
        assert not (tmp_path / "var" / "ventures").exists()
        with pytest.raises(ValidationError, match="Gate"):
            company.record_approval_and_scaffold(
                outcome.contract_id,
                approval_status="NOT_REQUIRED",
                idempotency_key="scaffold",
            )


def test_council_conflict_creates_inbox_without_majority_fact(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic conflict.", idempotency_key="i")
        company.prepare_council(idea.id)
        for fake in (FakeCTO(), FakeCPO(), FakeCMO()):
            payload = fake.response(idea)
            if fake.role == "cmo":
                payload = deepcopy(payload)
                payload["contract_contribution"]["decision_level"] = "FP_FULL"
            response = tmp_path / f"{fake.role}.json"
            atomic_write_json(response, payload)
            company.ingest_council_response(
                idea.id, role=fake.role, response_file=response
            )

        with pytest.raises(ConflictError, match="conflict"):
            company.compile_council(idea.id, idempotency_key="compile")

        assert (
            tmp_path
            / "var"
            / "inbox"
            / "ideas"
            / idea.id
            / "council_conflict.json"
        ).is_file()
        assert "COUNCIL_CONFLICT_DETECTED" in {
            event["event_type"] for event in company.events()
        }
        inbox = company.inbox()
        assert inbox["file_items"][0]["payload"]["status"] == (
            "CEO_DECISION_REQUIRED"
        )


def test_idea_command_is_idempotent_and_rejects_payload_change(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as company:
        first = company.create_idea("Synthetic one.", idempotency_key="same-key")
        replay = company.create_idea("Synthetic one.", idempotency_key="same-key")
        assert replay.id == first.id
        with pytest.raises(ConflictError):
            company.create_idea("Synthetic two.", idempotency_key="same-key")


def test_equivalent_compile_with_a_new_key_reuses_contract(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic repeat compile.", idempotency_key="i")
        company.prepare_council(idea.id)
        for fake in (FakeCTO(), FakeCPO(), FakeCMO()):
            response = fake.write_response(company.root, idea)
            company.ingest_council_response(
                idea.id, role=fake.role, response_file=response
            )

        first = company.compile_council(idea.id, idempotency_key="compile-1")
        second = company.compile_council(idea.id, idempotency_key="compile-2")

        assert second.contract_id == first.contract_id
        assert company.store.scalar("SELECT count(*) FROM contracts") == 1
