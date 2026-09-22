from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.cli import main
from company_os.errors import ConflictError, ValidationError
from company_os.fakes import FakeCMO, FakeCPO, FakeCTO, FakeExecutor
from company_os.source_snapshot import SourceSnapshot
from company_os.utils import atomic_write_json, payload_hash, read_json

from .helpers import CleanSourceSnapshotter, build_venture


@dataclass
class MutableSnapshot:
    snapshot: SourceSnapshot

    def capture(self) -> SourceSnapshot:
        return self.snapshot


def _snapshot(commit: str, tree: str) -> SourceSnapshot:
    return SourceSnapshot(
        source_commit=commit,
        source_tree_oid="c" * 40,
        source_tree_sha256=tree,
        dirty=False,
    )


def _council_payloads(idea) -> dict[str, dict]:
    return {
        fake.role: fake.response(idea)
        for fake in (FakeCTO(), FakeCPO(), FakeCMO())
    }


def _ingest_council(
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


def _review_result(review, changes: list[dict]) -> dict:
    request = read_json(review.json_path)
    return {
        "schema_version": 2,
        "review_request_id": review.id,
        "review_request_hash": review.request_hash,
        "reviewed_commit": request["source_commit"],
        "reviewed_tree_sha256": request["source_tree_sha256"],
        "source": "user_supplied",
        "verdict": "CHANGES_REQUIRED",
        "findings": [],
        "required_changes": changes,
    }


def test_synthetic_fixture_cannot_support_arbitrary_fact_but_ceo_evidence_can(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea(
            "Evaluate a synthetic Korean tutoring market claim.",
            idempotency_key="i",
        )
        company.prepare_council(idea.id)
        payloads = _council_payloads(idea)
        fabricated = "The Korean AI tutoring market is KRW 10 trillion annually."
        payloads["cto"]["contract_contribution"]["claims"][0].update(
            statement=fabricated,
            evidence_refs=["source-evidence-1"],
            source_types=["SYNTHETIC_FIXTURE"],
        )
        _ingest_council(company, idea, payloads, tmp_path / "responses-v1")

        rejected = company.compile_council(idea.id, idempotency_key="compile-v1")
        assert rejected.gate_result.passed is False
        assert "FACT_EVIDENCE_SCOPE_MISMATCH" in {
            item.code for item in rejected.gate_result.violations
        }

        evidence_file = tmp_path / "ceo-market-source.txt"
        evidence_file.write_text("CEO-supplied source document.\n", encoding="utf-8")
        registered = company.register_idea_evidence(
            idea.id,
            evidence_file=evidence_file,
            external_ref="ceo-market-source",
            idempotency_key="register-market-source",
        )
        replacement = deepcopy(payloads["cto"])
        replacement["contract_contribution"]["claims"][0].update(
            evidence_refs=[registered["external_ref"]],
            source_types=["USER_SUPPLIED_DOCUMENT"],
        )
        replacement_path = tmp_path / "cto-v2.json"
        atomic_write_json(replacement_path, replacement)
        company.ingest_council_response(
            idea.id,
            role="cto",
            response_file=replacement_path,
        )

        accepted = company.compile_council(idea.id, idempotency_key="compile-v2")
        assert accepted.gate_result.passed is True
        accepted_row = company.store.get_row("contracts", accepted.contract_id)
        assert accepted_row is not None
        assert accepted_row["gate_status"] == "PASSED"
        evidence_row = company.store.get_row("evidence", registered["id"])
        assert evidence_row is not None
        assert evidence_row["kind"] == "CEO_SUPPLIED_DOCUMENT"
        assert evidence_row["trusted"] == 1
        assert evidence_row["sha256"] == registered["sha256"]

        canonical_copy = company._absolute(evidence_row["path"])
        original_bytes = canonical_copy.read_bytes()
        canonical_copy.write_text("tampered registered evidence\n", encoding="utf-8")
        with pytest.raises(ConflictError, match="different command or payload"):
            company.compile_council(idea.id, idempotency_key="compile-v2")
        tampered = company.compile_council(idea.id, idempotency_key="compile-v3")
        assert tampered.gate_result.passed is False
        assert "FACT_EVIDENCE_NOT_TRUSTED" in {
            item.code for item in tampered.gate_result.violations
        }
        assert tampered.contract_id == accepted.contract_id
        tampered_row = company.store.get_row("contracts", accepted.contract_id)
        assert tampered_row is not None
        assert tampered_row["gate_status"] == "FAILED"
        assert company.store.get_row("ideas", idea.id)["status"] == "GATE_FAILED"
        canonical_copy.write_bytes(original_bytes)

        recovered = company.compile_council(idea.id, idempotency_key="compile-v4")
        assert recovered.gate_result.passed is True
        assert recovered.contract_id == accepted.contract_id
        recovered_row = company.store.get_row("contracts", accepted.contract_id)
        assert recovered_row is not None
        assert recovered_row["gate_status"] == "PASSED"
        assert company.store.get_row("ideas", idea.id)["status"] == "CONTRACT_COMPILED"

        reevaluations = [
            event
            for event in company.events()
            if event["event_type"] == "FIRST_PRINCIPLES_GATE_EVALUATED"
            and event["aggregate_id"] == accepted.contract_id
            and event["payload"].get("reevaluation") is True
        ]
        assert [event["payload"]["gate_status"] for event in reevaluations] == [
            "FAILED",
            "PASSED",
        ]


def test_idea_evidence_alias_cannot_shadow_an_internal_evidence_id(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic Evidence alias collision.", idempotency_key="i")
        company.prepare_council(idea.id)
        fixture = company.store.query_one(
            "SELECT id FROM evidence WHERE idea_id = ? AND kind = 'SYNTHETIC_FIXTURE'",
            (idea.id,),
        )
        assert fixture is not None
        source = tmp_path / "ceo-source.txt"
        source.write_text("CEO supplied Evidence.\n", encoding="utf-8")

        with pytest.raises(ValidationError, match="collides"):
            company.register_idea_evidence(
                idea.id,
                evidence_file=source,
                external_ref=str(fixture["id"]),
                idempotency_key="collision",
            )


def test_synthetic_fixture_internal_id_cannot_shadow_an_existing_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic generated ID collision.", idempotency_key="i")
        source = tmp_path / "ceo-source.txt"
        source.write_text("CEO supplied Evidence.\n", encoding="utf-8")
        colliding_id = "evidence_11111111111111111111111111111111"
        company.register_idea_evidence(
            idea.id,
            evidence_file=source,
            external_ref=colliding_id,
            idempotency_key="register",
        )
        generated_ids = iter(
            [colliding_id, "evidence_22222222222222222222222222222222"]
        )
        monkeypatch.setattr(
            "company_os.application.new_id",
            lambda prefix: next(generated_ids),
        )

        company.prepare_council(idea.id)

        fixture = company.store.query_one(
            "SELECT id FROM evidence WHERE idea_id = ? AND kind = 'SYNTHETIC_FIXTURE'",
            (idea.id,),
        )
        assert fixture is not None
        assert fixture["id"] == "evidence_22222222222222222222222222222222"
        assert colliding_id in company._evidence_grants(idea.id)
        assert fixture["id"] in company._evidence_grants(idea.id)


def test_registered_evidence_internal_id_cannot_equal_its_own_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic self alias collision.", idempotency_key="i")
        source = tmp_path / "ceo-source.txt"
        source.write_text("CEO supplied Evidence.\n", encoding="utf-8")
        colliding_id = "evidence_33333333333333333333333333333333"
        replacement_id = "evidence_44444444444444444444444444444444"
        generated_ids = iter([colliding_id, replacement_id])
        monkeypatch.setattr(
            "company_os.application.new_id",
            lambda prefix: next(generated_ids),
        )

        registered = company.register_idea_evidence(
            idea.id,
            evidence_file=source,
            external_ref=colliding_id,
            idempotency_key="register",
        )

        assert registered["id"] == replacement_id
        grants = company._evidence_grants(idea.id)
        assert colliding_id in grants
        assert replacement_id in grants


def test_cli_default_compile_key_rechecks_tamper_and_restoration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic CLI Evidence replay.", idempotency_key="i")
        company.prepare_council(idea.id)
        source = tmp_path / "ceo-source.txt"
        source.write_text("CEO supplied Evidence.\n", encoding="utf-8")
        registered = company.register_idea_evidence(
            idea.id,
            evidence_file=source,
            external_ref="ceo-cli-replay",
            idempotency_key="register",
        )
        payloads = _council_payloads(idea)
        payloads["cto"]["contract_contribution"]["claims"][0].update(
            statement="A CEO document supports this synthetic CLI claim.",
            evidence_refs=[registered["external_ref"]],
            source_types=["USER_SUPPLIED_DOCUMENT"],
        )
        _ingest_council(company, idea, payloads, tmp_path / "responses")
        evidence_row = company.store.get_row("evidence", registered["id"])
        assert evidence_row is not None
        canonical_copy = company._absolute(evidence_row["path"])
        original_bytes = canonical_copy.read_bytes()

    command = ("--root", str(tmp_path), "council", "compile", idea.id)
    assert main(command) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["gate"]["passed"] is True

    canonical_copy.write_text("tampered registered Evidence\n", encoding="utf-8")
    assert main(command) == 0
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["gate"]["passed"] is False

    canonical_copy.write_bytes(original_bytes)
    assert main(command) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["gate"]["passed"] is True
    assert recovered["contract_id"] == accepted["contract_id"]

    with CompanyOS(tmp_path) as restored:
        contract = restored.store.get_row("contracts", accepted["contract_id"])
        assert contract is not None
        assert contract["gate_status"] == "PASSED"
        assert restored.store.get_row("ideas", idea.id)["status"] == "CONTRACT_COMPILED"


def test_scaffold_rejects_tampered_unused_trusted_idea_evidence(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic scaffold Evidence integrity.", idempotency_key="i")
        company.prepare_council(idea.id)
        source = tmp_path / "unused-ceo-source.txt"
        source.write_text("Unused but trusted CEO Evidence.\n", encoding="utf-8")
        registered = company.register_idea_evidence(
            idea.id,
            evidence_file=source,
            external_ref="unused-ceo",
            idempotency_key="register",
        )
        _ingest_council(
            company,
            idea,
            _council_payloads(idea),
            tmp_path / "responses",
        )
        outcome = company.compile_council(idea.id, idempotency_key="compile")
        assert outcome.gate_result.passed is True

        evidence_row = company.store.get_row("evidence", registered["id"])
        assert evidence_row is not None
        company._absolute(evidence_row["path"]).write_text(
            "tampered unused CEO Evidence\n",
            encoding="utf-8",
        )

        with pytest.raises(ValidationError, match="Trusted Idea Evidence failed"):
            company.record_approval_and_scaffold(
                outcome.contract_id,
                approval_status="NOT_REQUIRED",
                idempotency_key="scaffold",
            )
        assert company.store.query_all("SELECT id FROM ventures") == []


def test_cli_can_register_hashed_ceo_idea_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea(
            "Register a synthetic CEO evidence document.",
            idempotency_key="idea",
        )
    source = tmp_path / "ceo-source.exe"
    source.write_text("Synthetic CEO evidence content.\n", encoding="utf-8")

    assert (
        main(
            (
                "--root",
                str(tmp_path),
                "evidence",
                "add",
                "--idea",
                idea.id,
                "--file",
                str(source),
                "--external-ref",
                "ceo-cli-source",
            )
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["external_ref"] == "ceo-cli-source"
    assert len(output["sha256"]) == 64
    assert output["path"].endswith("source.txt")
    with CompanyOS(tmp_path) as restored:
        row = restored.store.get_row("evidence", output["id"])
        assert row is not None
        assert row["sha256"] == output["sha256"]


def test_repair_requires_change_specific_test_result_bound_to_source(
    tmp_path: Path,
) -> None:
    source = MutableSnapshot(_snapshot("a" * 40, "b" * 64))
    with CompanyOS(tmp_path, source_snapshotter=source) as company:
        _, _, _, work_order = build_venture(company, "test-result-evidence")
        initial_run = company.execute_work_order(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="initial-run",
        )
        review = company.prepare_review(work_order.id, idempotency_key="review")
        changes = [
            {"id": "CHANGE-A", "description": "Verify behavior A."},
            {"id": "CHANGE-B", "description": "Verify behavior B."},
        ]
        result_path = tmp_path / "changes-required.json"
        atomic_write_json(result_path, _review_result(review, changes))
        company.ingest_review_result(review.id, result_path)
        source.snapshot = _snapshot("d" * 40, "e" * 64)

        verifier_evidence = next(
            item
            for item in company.evidence_for_run(initial_run.id)
            if item.kind == "VERIFIER_OUTPUT"
        )
        artifact_only_manifest = {
            "schema_version": 1,
            "review_id": review.id,
            "source_commit": source.snapshot.source_commit,
            "source_tree_sha256": source.snapshot.source_tree_sha256,
            "changes": [
                {
                    "id": change["id"],
                    "commit": source.snapshot.source_commit,
                    "evidence_ids": [verifier_evidence.id],
                }
                for change in changes
            ],
        }
        with pytest.raises(ValidationError, match="TEST_RESULT"):
            company._validate_repair_manifest(work_order.id, artifact_only_manifest)

        failed_report = tmp_path / "failed-result.json"
        atomic_write_json(
            failed_report,
            {
                "schema_version": 1,
                "kind": "PYTEST_RESULT",
                "status": "FAILED",
                "exit_code": 1,
                "source_commit": source.snapshot.source_commit,
                "source_tree_sha256": source.snapshot.source_tree_sha256,
                "tests": [
                    {
                        "node_id": "tests/test_repairs.py::failed",
                        "outcome": "FAILED",
                    }
                ],
            },
        )
        with pytest.raises(ValidationError, match="status must be PASSED"):
            company.register_test_result(
                work_order.id,
                required_change_id="CHANGE-A",
                result_file=failed_report,
                test_node_ids=["tests/test_repairs.py::failed"],
                idempotency_key="failed-test-result",
                source_commit=source.snapshot.source_commit,
            )

        test_result_ids: dict[str, str] = {}
        for change in changes:
            hostile_suffix = ".xml" if change["id"] == "CHANGE-A" else ".exe"
            report = tmp_path / f"{change['id']}-result{hostile_suffix}"
            node_id = f"tests/test_repairs.py::{change['id']}"
            atomic_write_json(
                report,
                {
                    "schema_version": 1,
                    "kind": "PYTEST_RESULT",
                    "status": "PASSED",
                    "exit_code": 0,
                    "source_commit": source.snapshot.source_commit,
                    "source_tree_sha256": source.snapshot.source_tree_sha256,
                    "tests": [{"node_id": node_id, "outcome": "PASSED"}],
                },
            )
            registered = company.register_test_result(
                work_order.id,
                required_change_id=change["id"],
                result_file=report,
                test_node_ids=[node_id],
                idempotency_key=f"test-result-{change['id']}",
                source_commit=source.snapshot.source_commit,
            )
            test_result_ids[change["id"]] = registered["id"]
            assert registered["path"].endswith(".json")

        source.snapshot = _snapshot("f" * 40, "1" * 64)
        replayed = company.register_test_result(
            work_order.id,
            required_change_id="CHANGE-A",
            result_file=tmp_path / "CHANGE-A-result.xml",
            test_node_ids=["tests/test_repairs.py::CHANGE-A"],
            idempotency_key="test-result-CHANGE-A",
            source_commit="d" * 40,
        )
        assert replayed["id"] == test_result_ids["CHANGE-A"]
        source.snapshot = _snapshot("d" * 40, "e" * 64)

        valid_manifest = deepcopy(artifact_only_manifest)
        for change in valid_manifest["changes"]:
            change["evidence_ids"].append(test_result_ids[change["id"]])
        normalized = company._validate_repair_manifest(work_order.id, valid_manifest)
        assert {
            change["id"]: change["evidence_ids"] for change in normalized["changes"]
        } == {
            change["id"]: sorted(
                [verifier_evidence.id, test_result_ids[change["id"]]]
            )
            for change in changes
        }

        reused = deepcopy(valid_manifest)
        reused["changes"][1]["evidence_ids"] = [
            verifier_evidence.id,
            test_result_ids["CHANGE-A"],
        ]
        with pytest.raises(ValidationError, match="required_change_id"):
            company._validate_repair_manifest(work_order.id, reused)

        change_b_row = company.store.get_row(
            "evidence", test_result_ids["CHANGE-B"]
        )
        assert change_b_row is not None
        change_b_path = company._absolute(change_b_row["path"])
        change_b_bytes = change_b_path.read_bytes()
        change_b_path.write_text("tampered test output\n", encoding="utf-8")
        with pytest.raises(ValidationError, match="missing or changed"):
            company._validate_repair_manifest(work_order.id, valid_manifest)
        change_b_path.write_bytes(change_b_bytes)

        row = company.store.get_row("evidence", test_result_ids["CHANGE-A"])
        assert row is not None
        tampered_payload = json.loads(row["payload_json"])
        tampered_payload["source_commit"] = "f" * 40
        company.store.update_row(
            "evidence",
            row["id"],
            {"payload_json": company._json(tampered_payload)},
        )
        with pytest.raises(ValidationError, match="source_commit"):
            company._validate_repair_manifest(work_order.id, valid_manifest)


def test_non_owner_council_opinion_is_hashed_in_provenance_and_inbox(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        idea = company.create_idea("Synthetic council dissent.", idempotency_key="i")
        company.prepare_council(idea.id)
        payloads = _council_payloads(idea)
        baseline_contribution = deepcopy(
            payloads["cto"]["contract_contribution"]
        )
        for payload in payloads.values():
            payload["contract_contribution"] = deepcopy(baseline_contribution)
        owner_value = payloads["cto"]["contract_contribution"]["pass_condition"]
        cpo_value = "CPO proposes a stricter product pass condition."
        payloads["cpo"]["contract_contribution"]["pass_condition"] = cpo_value
        _ingest_council(company, idea, payloads, tmp_path / "responses")

        outcome = company.compile_council(idea.id, idempotency_key="compile")
        contract = json.loads(
            company.store.get_row("contracts", outcome.contract_id)["payload_json"]
        )
        provenance = contract["council_provenance"]

        assert contract["pass_condition"] == owner_value
        assert provenance["ignored_fields"]["cpo"]["pass_condition"] == payload_hash(
            cpo_value
        )
        conflict = next(
            item
            for item in provenance["non_owner_conflicts"]
            if item["field"] == "pass_condition" and item["opinion_role"] == "cpo"
        )
        assert conflict["owner_role"] == "cto"
        assert conflict["owner_value_hash"] == payload_hash(owner_value)
        assert conflict["opinion_value_hash"] == payload_hash(cpo_value)
        assert cpo_value not in json.dumps(provenance)

        item = next(
            item
            for item in company.inbox()["file_items"]
            if item["payload"].get("type") == "COUNCIL_NON_OWNER_CONFLICT"
        )
        assert item["payload"]["status"] == "CEO_REVIEW_REQUIRED"
        assert item["payload"]["contract_id"] == outcome.contract_id
        inbox_conflict = next(
            conflict
            for conflict in item["payload"]["conflicts"]
            if conflict["field"] == "pass_condition"
            and conflict["opinion_role"] == "cpo"
        )
        assert inbox_conflict["opinion_value"] == cpo_value
        detected_event = next(
            event
            for event in company.events()
            if event["event_type"] == "COUNCIL_NON_OWNER_CONFLICT_DETECTED"
        )
        assert detected_event["payload"]["contract_id"] == outcome.contract_id

        resolved_cpo = deepcopy(payloads["cpo"])
        resolved_cpo["contract_contribution"]["pass_condition"] = owner_value
        resolved_path = tmp_path / "cpo-resolved.json"
        atomic_write_json(resolved_path, resolved_cpo)
        company.ingest_council_response(
            idea.id,
            role="cpo",
            response_file=resolved_path,
        )
        resolved_outcome = company.compile_council(
            idea.id,
            idempotency_key="compile-resolved",
        )
        resolved_item = next(
            item
            for item in company.inbox()["file_items"]
            if item["payload"].get("type") == "COUNCIL_NON_OWNER_CONFLICT"
        )
        assert resolved_item["payload"]["status"] == "RESOLVED"
        assert resolved_item["payload"]["contract_id"] == resolved_outcome.contract_id
        assert resolved_item["payload"]["conflicts"] == []


def test_review_response_schema_declares_exact_v2_source_binding(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path, source_snapshotter=CleanSourceSnapshotter()) as company:
        _, _, _, work_order = build_venture(company, "response-schema-constants")
        company.execute_work_order(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="run",
        )
        review = company.prepare_review(work_order.id, idempotency_key="review")
        request = read_json(review.json_path)

        assert request["response_schema"]["required_values"] == {
            "schema_version": 2,
            "reviewed_commit": request["source_commit"],
            "reviewed_tree_sha256": request["source_tree_sha256"],
        }
