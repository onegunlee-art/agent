from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from company_os.ledger_backup import backup, migrate_ledger, restore, verify
from company_os.paths import default_ledger_path, validate_live_db_path
from company_os.storage import SQLiteStateStore


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
