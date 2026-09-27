from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import company_os.customer_isolation as isolation
from company_os.application import CompanyOS
from company_os.audit_events import (
    SOURCE_PUSH_APPROVAL_TEXT,
    record_claude_verdict_archived,
    record_source_pushed,
)
from company_os.customer_isolation import (
    delete_client_data,
    initialize_client,
    recover_client_deletion,
    sync_client_templates,
    template_sync_approval_text,
)
from company_os.dashboard import _cli_action_runner
from company_os.errors import ValidationError
from company_os.utils import sha256_file


def _seed_templates(root: Path, version: str = "one") -> None:
    for relative in (
        "lines/chatbot/WORK_ORDER_TEMPLATE.md",
        "src/company_os/synthetic_faq.py",
        "tests/test_synthetic_faq.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{version}: {relative}\n", encoding="utf-8")


def _client(company: CompanyOS, private: Path) -> Path:
    _seed_templates(company.root)
    initialize_client(
        company,
        private,
        "client-alpha",
        private_markers=["ALPHA-PRIVATE-41"],
        customer_markers=["Alpha Synthetic Ltd"],
        token_limit=1000,
        cost_limit_usd=1.0,
        idempotency_key="init-alpha",
    )
    return private / "clients" / "client-alpha"


def test_interrupted_deletion_can_be_truthfully_finalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / "private"
    with CompanyOS(tmp_path / "public") as company:
        client = _client(company, private)
        (private / "backups" / "client-alpha").mkdir(parents=True)
        (private / "backups" / "client-alpha" / "copy.zip").write_bytes(b"backup")
        original = company.store.append_event

        def interrupt(event_type: str, **kwargs):
            if event_type == "CUSTOMER_DATA_DELETED":
                raise RuntimeError("synthetic interruption after filesystem deletion")
            return original(event_type, **kwargs)

        monkeypatch.setattr(company.store, "append_event", interrupt)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            delete_client_data(
                company,
                private,
                "client-alpha",
                confirmation="client-alpha",
                idempotency_key="delete-alpha",
            )
        assert not client.exists()
        monkeypatch.setattr(company.store, "append_event", original)

        result = recover_client_deletion(
            company,
            private,
            "client-alpha",
            deletion_idempotency_key="delete-alpha",
        )

        assert result["status"] == "DELETED"
        assert result["recovered"] is True
        assert company.store.get_row("idempotency", "delete-alpha")["status"] == "COMPLETED"
        assert company.events()[-1]["event_type"] == "CUSTOMER_DATA_DELETED"


def test_interrupted_deletion_with_residual_data_becomes_retryable(tmp_path: Path) -> None:
    private = tmp_path / "private"
    with CompanyOS(tmp_path / "public") as company:
        _client(company, private)
        company.store.claim_idempotency(
            "delete-alpha",
            "delete_client_data",
            {"client_id": "client-alpha", "pre_delete_tree_sha256": "a" * 64},
        )

        result = recover_client_deletion(
            company,
            private,
            "client-alpha",
            deletion_idempotency_key="delete-alpha",
        )

        assert result == {"status": "RETRY_ALLOWED", "client_id": "client-alpha"}
        assert company.store.get_row("idempotency", "delete-alpha") is None


def test_template_sync_requires_hash_bound_ceo_approval_and_records_trees(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    with CompanyOS(tmp_path / "public") as company:
        client = _client(company, private)
        _seed_templates(company.root, version="two")
        approval = tmp_path / "approval.txt"
        approval.write_text(
            template_sync_approval_text(company.root, private, "client-alpha") + "\n",
            encoding="utf-8",
        )

        result = sync_client_templates(
            company,
            private,
            "client-alpha",
            approval_file=approval,
            idempotency_key="sync-alpha",
        )

        assert result["before_tree_oid"] != result["after_tree_oid"]
        assert (client / "lines/chatbot/WORK_ORDER_TEMPLATE.md").read_text(
            encoding="utf-8"
        ).startswith("two:")
        event_types = [event["event_type"] for event in company.events()]
        assert "CLIENT_TEMPLATE_SYNC_APPROVED" in event_types
        assert "CLIENT_TEMPLATES_SYNCED" in event_types

        approval.write_text("not approved\n", encoding="utf-8")
        with pytest.raises(ValidationError, match="approval"):
            sync_client_templates(
                company,
                private,
                "client-alpha",
                approval_file=approval,
                idempotency_key="sync-alpha-invalid",
            )


@pytest.mark.parametrize("folder", ["OneDrive", "Dropbox", "Google Drive", "iCloud"])
def test_known_sync_folders_are_rejected(tmp_path: Path, folder: str) -> None:
    with CompanyOS(tmp_path / "public") as company:
        _seed_templates(company.root)
        with pytest.raises(ValidationError, match="sync folder"):
            initialize_client(
                company,
                tmp_path / folder / "registry",
                "client-alpha",
                private_markers=["PRIVATE"],
                customer_markers=["CUSTOMER"],
                token_limit=100,
                cost_limit_usd=1.0,
                idempotency_key=f"sync-{folder}",
            )


def test_extra_sync_folder_names_are_configurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_COMPANY_OS_EXTRA_SYNC_FOLDERS", "Acme Sync;Other Cloud")
    with CompanyOS(tmp_path / "public") as company:
        _seed_templates(company.root)
        with pytest.raises(ValidationError, match="sync folder"):
            initialize_client(
                company,
                tmp_path / "Acme Sync" / "registry",
                "client-alpha",
                private_markers=["PRIVATE"],
                customer_markers=["CUSTOMER"],
                token_limit=100,
                cost_limit_usd=1.0,
                idempotency_key="custom-sync-folder",
            )


def test_dashboard_cli_runner_injects_repo_src_into_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, '{"status":"RECORDED"}', "")

    monkeypatch.setattr("company_os.dashboard.subprocess.run", fake_run)
    result = _cli_action_runner(root, tmp_path / "ledger.sqlite3")(
        "approve", "work-order", "", "key"
    )

    assert result["status"] == "RECORDED"
    assert str(root / "src") in str(captured["env"]["PYTHONPATH"])


def test_dedicated_audit_events_are_hash_bound_and_not_generic(tmp_path: Path) -> None:
    root = tmp_path / "company"
    verdict = tmp_path / "verdict.md"
    verdict.write_text("synthetic PASS\n", encoding="utf-8")
    approval = tmp_path / "push-approval.txt"
    approval.write_text(SOURCE_PUSH_APPROVAL_TEXT + "\n", encoding="utf-8")
    with CompanyOS(root) as company:
        archived = record_claude_verdict_archived(
            company,
            verdict,
            expected_sha256=sha256_file(verdict),
            provenance="ORIGINAL",
            idempotency_key="archive-verdict",
        )
        pushed = record_source_pushed(
            company,
            remote_url="https://example.invalid/company.git",
            commit="a" * 40,
            tags=["v0.5.0"],
            approval_file=approval,
            idempotency_key="record-push",
        )
        event_types = [event["event_type"] for event in company.events()]

    assert archived["event_type"] == "CLAUDE_VERDICT_ARCHIVED"
    assert pushed["event_type"] == "SOURCE_PUSHED"
    assert event_types[-2:] == ["CLAUDE_VERDICT_ARCHIVED", "SOURCE_PUSHED"]
