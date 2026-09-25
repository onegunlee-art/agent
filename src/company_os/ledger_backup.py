"""SQLite online backup, verification, migration, and separate restore."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BackupResult:
    path: Path
    sha256: str
    table_counts: dict[str, int]


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    hash_matches: bool
    integrity: str
    table_counts: dict[str, int]
    counts_match: bool


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    return {
        name: int(
            connection.execute(
                f'SELECT COUNT(*) FROM "{name.replace(chr(34), chr(34) * 2)}"'
            ).fetchone()[0]
        )
        for name in names
    }


def _write_sidecar(path: Path, digest: str) -> None:
    path.with_suffix(".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8"
    )


def _online_copy(source: Path, target: Path) -> dict[str, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
        return table_counts(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


def backup(
    db_path: str | Path,
    backup_dir: str | Path,
    *,
    stem: str = "ledger",
    timestamp: str | None = None,
) -> BackupResult:
    source = Path(db_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    directory = Path(backup_dir).resolve()
    stamp = timestamp or time.strftime("%Y%m%d-%H%M%S")
    target = directory / f"{stem}-{stamp}.sqlite3"
    suffix = 0
    while target.exists():
        suffix += 1
        target = directory / f"{stem}-{stamp}-{suffix:02d}.sqlite3"
    counts = _online_copy(source, target)
    digest = sha256_file(target)
    _write_sidecar(target, digest)
    return BackupResult(target, digest, counts)


def verify(
    backup_path: str | Path,
    *,
    expected_counts: dict[str, int] | None = None,
) -> VerificationResult:
    source = Path(backup_path).resolve()
    sidecar = source.with_suffix(".sha256")
    if not source.is_file() or not sidecar.is_file():
        return VerificationResult(False, False, "missing", {}, False)
    recorded_parts = sidecar.read_text(encoding="utf-8").split()
    recorded = recorded_parts[0] if recorded_parts else ""
    hash_matches = recorded == sha256_file(source)
    integrity = "not_checked"
    counts: dict[str, int] = {}
    if hash_matches:
        connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity == "ok":
                counts = table_counts(connection)
        except sqlite3.DatabaseError:
            integrity = "database_error"
        finally:
            connection.close()
    counts_match = expected_counts is None or counts == expected_counts
    return VerificationResult(
        hash_matches and integrity == "ok" and counts_match,
        hash_matches,
        integrity,
        counts,
        counts_match,
    )


def restore(backup_path: str | Path, new_db_path: str | Path) -> dict[str, int]:
    source = Path(backup_path).resolve()
    target = Path(new_db_path).resolve()
    checked = verify(source)
    if not checked.ok:
        raise RuntimeError("backup verification failed; restore aborted")
    if target.exists():
        raise FileExistsError(f"restore target already exists: {target}")
    return _online_copy(source, target)


def migrate_ledger(source_path: str | Path, target_path: str | Path) -> dict[str, int]:
    source = Path(source_path).resolve()
    target = Path(target_path).resolve()
    if target.exists():
        raise FileExistsError(f"migration target already exists: {target}")
    counts = _online_copy(source, target)
    digest = sha256_file(target)
    _write_sidecar(target, digest)
    checked = verify(target, expected_counts=counts)
    if not checked.ok:
        raise RuntimeError("migrated ledger verification failed")
    return counts
