from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.cli import build_parser
from company_os.fakes import FakeExecutor
from company_os.ledger_backup import (
    backup,
    backup_recovery_bundle,
    migrate_ledger,
    restore,
    restore_recovery_bundle,
    sha256_file,
    verify,
    verify_recovery_bundle,
)
from company_os.paths import default_ledger_path, validate_live_db_path
from company_os.storage import SQLiteStateStore

from .helpers import CleanSourceSnapshotter, build_venture


def _ledger(path: Path) -> None:
    with SQLiteStateStore(path) as store:
        store.set_global_state("synthetic", {"value": 7})


def test_online_backup_verify_and_separate_restore(tmp_path: Path) -> None:
    source = tmp_path / "ledger.sqlite3"
    _ledger(source)
    result = backup(source, tmp_path / "backups", timestamp="20260925-120000")
    assert result.path.is_file()
    assert result.path.with_suffix(".sha256").is_file()
    checked = verify(result.path, expected_counts=result.table_counts)
    assert checked.ok and checked.integrity == "ok"

    restored = tmp_path / "restore" / "ledger.sqlite3"
    restored_counts = restore(result.path, restored)
    assert restored_counts == result.table_counts
    connection = sqlite3.connect(restored)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_tampered_backup_and_overwrite_restore_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "ledger.sqlite3"
    _ledger(source)
    result = backup(source, tmp_path / "backups", timestamp="20260925-120001")
    with result.path.open("r+b") as handle:
        handle.seek(100)
        handle.write(b"\x00" * 8)
    assert verify(result.path).ok is False
    with pytest.raises(RuntimeError, match="verification"):
        restore(result.path, tmp_path / "restored.sqlite3")


def test_legacy_migration_preserves_source_and_counts(tmp_path: Path) -> None:
    source = tmp_path / "legacy" / "company.db"
    source.parent.mkdir()
    _ledger(source)
    target = tmp_path / "canonical" / "ledger.sqlite3"
    counts = migrate_ledger(source, target)
    assert source.is_file() and target.is_file()
    assert verify(target, expected_counts=counts).ok
    with pytest.raises(FileExistsError):
        migrate_ledger(source, target)


def test_default_path_is_outside_git_repo_and_onedrive_live_db_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    local_data = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local_data))
    path = default_ledger_path(repo)
    assert repo not in path.parents
    assert local_data in path.parents
    with pytest.raises(ValueError, match="synchron"):
        validate_live_db_path(tmp_path / "OneDrive" / "ledger.sqlite3")


def test_recovery_bundle_restores_run_evidence_hash_and_allows_next_work(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source-company"
    source_db = tmp_path / "source-state" / "ledger.sqlite3"
    with CompanyOS(
        source_root,
        db_path=source_db,
        source_snapshotter=CleanSourceSnapshotter(),
    ) as company:
        _, _, _, work_order = build_venture(company, "recovery-bundle")
        run = company.execute_work_order(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="recovery-bundle-run",
        )
        evidence = company.evidence_for_run(run.id)
        assert evidence

        bundle = backup_recovery_bundle(
            source_db,
            source_root,
            tmp_path / "backups",
            timestamp="20260925-130000",
        )

    checked = verify_recovery_bundle(bundle.path)
    assert checked.ok
    assert checked.file_count >= len(evidence)

    restored_root = tmp_path / "restored-company"
    restored_db = tmp_path / "restored-state" / "ledger.sqlite3"
    restored = restore_recovery_bundle(
        bundle.path,
        new_db_path=restored_db,
        new_root=restored_root,
    )
    assert restored.db_path == restored_db.resolve()

    with CompanyOS(
        restored_root,
        db_path=restored_db,
        source_snapshotter=CleanSourceSnapshotter(),
    ) as company:
        assert company.run(run.id).status == "PASS"
        assert company.work_order(work_order.id).status == "VERIFIED"
        restored_evidence = company.evidence_for_run(run.id)
        assert [item.id for item in restored_evidence] == [
            item.id for item in evidence
        ]
        for item in restored_evidence:
            assert item.path.is_file()
            assert sha256_file(item.path) == item.sha256

        next_idea = company.create_idea(
            "Continue after a full synthetic recovery.",
            idempotency_key="recovery-bundle-next-work",
        )
        assert company.idea(next_idea.id).text == next_idea.text


def test_recovery_bundle_rejects_missing_referenced_file(tmp_path: Path) -> None:
    source_root = tmp_path / "source-company"
    source_db = tmp_path / "source-state" / "ledger.sqlite3"
    with CompanyOS(
        source_root,
        db_path=source_db,
        source_snapshotter=CleanSourceSnapshotter(),
    ) as company:
        _, _, venture, _ = build_venture(company, "missing-recovery-file")
        venture.context_manifest_path.unlink()
        with pytest.raises(FileNotFoundError, match="referenced runtime file"):
            backup_recovery_bundle(
                source_db,
                source_root,
                tmp_path / "backups",
                timestamp="20260925-130001",
            )


def test_cli_exposes_full_recovery_bundle_commands() -> None:
    parser = build_parser()
    backup_args = parser.parse_args(
        ["ledger", "recovery-backup", "--dir", "backups"]
    )
    verify_args = parser.parse_args(
        ["ledger", "recovery-verify", "--bundle", "backup.zip"]
    )
    restore_args = parser.parse_args(
        [
            "ledger",
            "recovery-restore",
            "--bundle",
            "backup.zip",
            "--to-db",
            "state/ledger.sqlite3",
            "--to-root",
            "restored-company",
        ]
    )
    assert backup_args.ledger_command == "recovery-backup"
    assert verify_args.ledger_command == "recovery-verify"
    assert restore_args.ledger_command == "recovery-restore"
