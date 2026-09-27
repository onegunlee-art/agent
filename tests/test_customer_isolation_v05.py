from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from company_os.application import CompanyOS
from company_os.cli import build_parser
from company_os.customer_isolation import (
    backup_client_data,
    client_sparse_patterns,
    configure_customer_sparse_checkout,
    delete_client_data,
    initialize_client,
    isolated_evaluation_draft,
    load_client_policy,
    restore_client_backup,
    save_isolated_evaluation_draft,
    validate_client_execution,
    verify_client_backup,
)
from company_os.errors import ValidationError
from company_os.model_executor import capture_workspace_identity
from company_os.utils import sha256_file


def _register_clients(company: CompanyOS, private_root: Path) -> None:
    initialize_client(
        company,
        private_root,
        "client-alpha",
        private_markers=["ALPHA-PRIVATE-41"],
        customer_markers=["Alpha Synthetic Ltd"],
        token_limit=120_000,
        cost_limit_usd=1.25,
        idempotency_key="client-alpha-init",
    )
    initialize_client(
        company,
        private_root,
        "client-beta",
        private_markers=["BETA-PRIVATE-77"],
        customer_markers=["Beta Synthetic Ltd"],
        token_limit=80_000,
        cost_limit_usd=0.75,
        idempotency_key="client-beta-init",
    )


def test_client_registration_keeps_raw_markers_out_of_public_ledger(
    tmp_path: Path,
) -> None:
    public_root = tmp_path / "public-company-os"
    private_root = tmp_path / "private-customer-repository"
    with CompanyOS(public_root) as company:
        _register_clients(company, private_root)

        policy = load_client_policy(private_root, "client-alpha", company=company)
        assert policy.token_limit == 120_000
        assert policy.cost_limit_usd == 1.25
        assert policy.client_root == private_root / "clients" / "client-alpha"
        event_text = json.dumps(company.events(), ensure_ascii=False)
        config_path = private_root / "clients" / "client-alpha" / "client.json"
        changed = json.loads(config_path.read_text(encoding="utf-8"))
        changed["token_limit"] = 999_999
        config_path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(ValidationError, match="ledger"):
            load_client_policy(private_root, "client-alpha", company=company)

    assert "ALPHA-PRIVATE-41" not in event_text
    assert "Alpha Synthetic Ltd" not in event_text
    assert "BETA-PRIVATE-77" not in event_text
    public_bytes = b"\n".join(
        path.read_bytes() for path in public_root.rglob("*") if path.is_file()
    )
    assert b"ALPHA-PRIVATE-41" not in public_bytes
    assert b"Alpha Synthetic Ltd" not in public_bytes
    assert b"BETA-PRIVATE-77" not in public_bytes
    assert (private_root / "clients" / "client-alpha" / "material").is_dir()
    assert (private_root / "clients" / "client-alpha" / "workspace").is_dir()


def test_customer_sparse_patterns_and_limits_fail_closed(tmp_path: Path) -> None:
    public_root = tmp_path / "public-company-os"
    private_root = tmp_path / "private-customer-repository"
    with CompanyOS(public_root) as company:
        _register_clients(company, private_root)

    policy = load_client_policy(private_root, "client-alpha")
    expected = client_sparse_patterns("client-alpha")
    validated = validate_client_execution(
        policy,
        repository=private_root,
        sparse_checkout_patterns=expected,
        token_limit=100_000,
        cost_limit_usd=1.0,
    )
    assert validated == expected

    with pytest.raises(ValidationError, match="sparse"):
        validate_client_execution(
            policy,
            repository=private_root,
            sparse_checkout_patterns=(*expected, "clients/client-beta"),
            token_limit=100_000,
            cost_limit_usd=1.0,
        )
    with pytest.raises(ValidationError, match="token"):
        validate_client_execution(
            policy,
            repository=private_root,
            sparse_checkout_patterns=expected,
            token_limit=120_001,
            cost_limit_usd=1.0,
        )
    with pytest.raises(ValidationError, match="cost"):
        validate_client_execution(
            policy,
            repository=private_root,
            sparse_checkout_patterns=expected,
            token_limit=100_000,
            cost_limit_usd=1.26,
        )


def test_sparse_checkout_physically_excludes_another_customer(tmp_path: Path) -> None:
    repository = tmp_path / "private-customer-repository"
    repository.mkdir()
    for relative in (
        "clients/client-alpha/material",
        "clients/client-beta/material",
        "lines/chatbot",
        "src/company_os",
        "tests",
    ):
        folder = repository / relative
        folder.mkdir(parents=True)
        (folder / "tracked.txt").write_text(f"synthetic {relative}", encoding="utf-8")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "synthetic@example.invalid"],
        ["git", "config", "user.name", "Synthetic Test"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "synthetic private baseline"],
    ):
        subprocess.run(
            command,
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

    configure_customer_sparse_checkout(
        repository, client_sparse_patterns("client-alpha")
    )
    identity = capture_workspace_identity(repository)

    assert identity.sparse_checkout_patterns == client_sparse_patterns("client-alpha")
    assert (repository / "clients" / "client-alpha").is_dir()
    assert not (repository / "clients" / "client-beta").exists()


def test_private_customer_root_cannot_be_inside_public_source(tmp_path: Path) -> None:
    public_root = tmp_path / "public-company-os"
    with CompanyOS(public_root) as company:
        with pytest.raises(ValidationError, match="outside public"):
            initialize_client(
                company,
                public_root / "clients-private",
                "client-alpha",
                private_markers=["SYNTHETIC-PRIVATE"],
                customer_markers=["Synthetic Client"],
                token_limit=100,
                cost_limit_usd=0.25,
                idempotency_key="invalid-inside-public",
            )


def test_other_customer_markers_are_automatically_critical_forbidden(
    tmp_path: Path,
) -> None:
    public_root = tmp_path / "public-company-os"
    private_root = tmp_path / "private-customer-repository"
    with CompanyOS(public_root) as company:
        _register_clients(company, private_root)

    customer_field = "customer" + "_name"
    intake = {
        "schema_version": 1,
        "customer_id": "client-alpha",
        customer_field: "Alpha synthetic help desk",
        "refusal_text": "The supplied material does not contain that answer.",
        "private_markers": [],
        "other_customer_markers": [],
        "faqs": [
            {
                "id": "faq-hours",
                "question": "When are you open?",
                "synonyms": ["open", "hours"],
                "answer": "Open from nine to five.",
                "source": "Synthetic hours sheet",
                "must_include_all": ["nine", "five"],
            }
        ],
        "refusal_questions": ["weather"] * 5,
        "price_unknown_questions": ["price"] * 5,
        "negative_questions": [
            {
                "question": f"not closed, hours {index}",
                "expected_source_id": "faq-hours",
                "negated_terms": ["closed"],
            }
            for index in range(5)
        ],
        "secret_questions": ["secret"] * 5,
        "cross_customer_questions": ["other customer"] * 5,
    }
    draft = isolated_evaluation_draft(private_root, "client-alpha", intake)

    assert set(draft["critical_forbidden"]) == {
        "ALPHA-PRIVATE-41",
        "BETA-PRIVATE-77",
        "Beta Synthetic Ltd",
    }
    assert "Alpha Synthetic Ltd" not in draft["critical_forbidden"]

    intake_path = private_root / "clients" / "client-alpha" / "material" / "intake.json"
    intake_path.write_text(json.dumps(intake), encoding="utf-8")
    output = (
        private_root
        / "clients"
        / "client-alpha"
        / "workspace"
        / "eval_cases.json"
    )
    saved = save_isolated_evaluation_draft(
        private_root, "client-alpha", intake_path, output
    )
    assert saved["case_count"] == len(draft["cases"])
    assert saved["critical_forbidden_count"] == 3
    assert json.loads(output.read_text(encoding="utf-8")) == draft
    with pytest.raises(ValidationError, match="client folder"):
        save_isolated_evaluation_draft(
            private_root,
            "client-alpha",
            intake_path,
            public_root / "eval_cases.json",
        )


def test_customer_backup_restores_folder_and_hashes(tmp_path: Path) -> None:
    public_root = tmp_path / "public-company-os"
    private_root = tmp_path / "private-customer-repository"
    with CompanyOS(public_root) as company:
        _register_clients(company, private_root)
    material = private_root / "clients" / "client-alpha" / "material" / "faq.txt"
    material.write_text("synthetic alpha FAQ", encoding="utf-8")

    bundle = backup_client_data(
        private_root,
        "client-alpha",
        private_root / "backups",
        timestamp="20260927-120000",
    )
    checked = verify_client_backup(bundle.path)
    assert checked.ok and checked.file_count >= 2

    restored_root = tmp_path / "restored-private-repository"
    restored = restore_client_backup(bundle.path, restored_root)
    restored_material = restored / "material" / "faq.txt"
    assert restored == restored_root / "clients" / "client-alpha"
    assert restored_material.read_text(encoding="utf-8") == "synthetic alpha FAQ"
    assert sha256_file(restored_material) == sha256_file(material)

    with bundle.path.open("r+b") as handle:
        handle.seek(40)
        handle.write(b"tampered")
    assert verify_client_backup(bundle.path).ok is False


def test_customer_delete_removes_active_and_backup_data_but_keeps_evidence(
    tmp_path: Path,
) -> None:
    public_root = tmp_path / "public-company-os"
    private_root = tmp_path / "private-customer-repository"
    with CompanyOS(public_root) as company:
        _register_clients(company, private_root)
        material = (
            private_root / "clients" / "client-alpha" / "material" / "private.txt"
        )
        material.write_text("ALPHA-PRIVATE-41", encoding="utf-8")
        backup_client_data(
            private_root,
            "client-alpha",
            private_root / "backups",
            timestamp="20260927-120001",
        )

        result = delete_client_data(
            company,
            private_root,
            "client-alpha",
            confirmation="client-alpha",
            idempotency_key="delete-client-alpha",
        )
        events = company.events()

    assert not (private_root / "clients" / "client-alpha").exists()
    assert not (private_root / "backups" / "client-alpha").exists()
    certificate = Path(result["certificate_path"])
    assert certificate.is_file()
    assert sha256_file(certificate) == result["certificate_sha256"]
    certificate_payload = json.loads(certificate.read_text(encoding="utf-8"))
    assert certificate_payload["status"] == "DELETED"
    assert certificate_payload["deleted_file_count"] >= 2
    event_payload = json.dumps(events[-1]["payload"], ensure_ascii=False)
    assert events[-1]["event_type"] == "CUSTOMER_DATA_DELETED"
    assert "ALPHA-PRIVATE-41" not in event_payload
    assert "ALPHA-PRIVATE-41" not in certificate.read_text(encoding="utf-8")


def test_client_cli_and_pilot_documents_exist() -> None:
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "client",
            "delete",
            "client-alpha",
            "--private-root",
            "private",
            "--confirm-client-id",
            "client-alpha",
            "--idempotency-key",
            "delete-alpha",
        ]
    )
    model = parser.parse_args(
        [
            "work",
            "model-run",
            "work-order",
            "--repository",
            "private",
            "--worktree",
            "worktree",
            "--branch",
            "wo/client-alpha",
            "--instructions-file",
            "instructions.txt",
            "--test-arg",
            "python",
            "--idempotency-key",
            "run-alpha",
            "--client-id",
            "client-alpha",
            "--private-root",
            "private",
        ]
    )
    evaluation = parser.parse_args(
        [
            "client",
            "evaluation-draft",
            "client-alpha",
            "--private-root",
            "private",
            "--intake",
            "private/clients/client-alpha/material/intake.json",
            "--output",
            "private/clients/client-alpha/workspace/eval_cases.json",
        ]
    )
    root = Path(__file__).resolve().parents[1]

    assert parsed.client_command == "delete"
    assert model.client_id == "client-alpha"
    assert evaluation.client_command == "evaluation-draft"
    assert (root / "docs" / "PRIVATE_CUSTOMER_REPOSITORY_RULES_KO.md").is_file()
    assert (root / "docs" / "V05_PILOT_CHECKLIST_KO.md").is_file()
