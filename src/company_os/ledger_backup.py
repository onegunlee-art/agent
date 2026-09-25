"""SQLite online backup, verification, migration, and separate restore."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


@dataclass(frozen=True)
class RecoveryBundleResult:
    path: Path
    sha256: str
    database_sha256: str
    table_counts: dict[str, int]
    file_count: int


@dataclass(frozen=True)
class RecoveryBundleVerification:
    ok: bool
    hash_matches: bool
    database_integrity: str
    database_hash_matches: bool
    counts_match: bool
    file_count: int
    missing_entries: tuple[str, ...]
    mismatched_entries: tuple[str, ...]


@dataclass(frozen=True)
class RecoveryRestoreResult:
    db_path: Path
    root: Path
    table_counts: dict[str, int]
    file_count: int


_REFERENCED_PATH_COLUMNS = (
    ("council_responses", "source_path"),
    ("ventures", "context_manifest_path"),
    ("work_orders", "verifier_path"),
    ("reviews", "request_json_path"),
    ("reviews", "request_markdown_path"),
    ("reviews", "response_path"),
    ("evidence", "path"),
    ("artifacts", "path"),
)


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


def _relative_runtime_path(root: Path, value: str) -> tuple[str, Path]:
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"referenced runtime file is outside company root: {value}"
            ) from exc
    else:
        relative = candidate
        resolved = (root / relative).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"referenced runtime file escapes company root: {value}"
            ) from exc
    if not resolved.is_file():
        raise FileNotFoundError(f"referenced runtime file is missing: {value}")
    return relative.as_posix(), resolved


def _referenced_runtime_files(
    connection: sqlite3.Connection,
    root: Path,
) -> list[tuple[str, Path]]:
    referenced: dict[str, Path] = {}
    for table, column in _REFERENCED_PATH_COLUMNS:
        rows = connection.execute(
            f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
        ).fetchall()
        for row in rows:
            value = str(row[0]).strip()
            if not value:
                continue
            relative, resolved = _relative_runtime_path(root, value)
            prior = referenced.get(relative)
            if prior is not None and prior != resolved:
                raise ValueError(f"ambiguous runtime path in ledger: {relative}")
            referenced[relative] = resolved
    return sorted(referenced.items())


def _bundle_manifest(
    database_path: Path,
    counts: dict[str, int],
    files: list[tuple[str, Path]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "database": {
            "archive_path": "ledger.sqlite3",
            "sha256": sha256_file(database_path),
            "size": database_path.stat().st_size,
            "table_counts": counts,
        },
        "files": [
            {
                "path": relative,
                "archive_path": f"files/{relative}",
                "sha256": sha256_file(source),
                "size": source.stat().st_size,
            }
            for relative, source in files
        ],
    }


def backup_recovery_bundle(
    db_path: str | Path,
    root: str | Path,
    backup_dir: str | Path,
    *,
    stem: str = "company-recovery",
    timestamp: str | None = None,
) -> RecoveryBundleResult:
    """Back up SQLite plus every filesystem record referenced by the ledger."""

    source_db = Path(db_path).resolve()
    company_root = Path(root).resolve()
    if not source_db.is_file():
        raise FileNotFoundError(source_db)
    if not company_root.is_dir():
        raise FileNotFoundError(company_root)
    directory = Path(backup_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = timestamp or time.strftime("%Y%m%d-%H%M%S")
    target = directory / f"{stem}-{stamp}.zip"
    suffix = 0
    while target.exists():
        suffix += 1
        target = directory / f"{stem}-{stamp}-{suffix:02d}.zip"

    with tempfile.TemporaryDirectory(prefix="company-recovery-") as temp_name:
        snapshot = Path(temp_name) / "ledger.sqlite3"
        counts = _online_copy(source_db, snapshot)
        connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        try:
            files = _referenced_runtime_files(connection, company_root)
        finally:
            connection.close()
        manifest = _bundle_manifest(snapshot, counts, files)
        manifest_bytes = json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        with zipfile.ZipFile(
            target,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            archive.writestr("manifest.json", manifest_bytes)
            archive.write(snapshot, "ledger.sqlite3")
            for relative, source in files:
                archive.write(source, f"files/{relative}")

    digest = sha256_file(target)
    _write_sidecar(target, digest)
    return RecoveryBundleResult(
        target,
        digest,
        str(manifest["database"]["sha256"]),
        counts,
        len(files),
    )


def _read_bundle_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ValueError("recovery bundle contains duplicate archive entries")
    if "manifest.json" not in names:
        raise ValueError("recovery bundle manifest is missing")
    manifest = json.loads(archive.read("manifest.json"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported recovery bundle manifest")
    return manifest


def _entry_bytes(archive: zipfile.ZipFile, name: str) -> bytes:
    path = Path(name.replace("/", os.sep))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe recovery bundle entry: {name}")
    return archive.read(name)


def verify_recovery_bundle(
    bundle_path: str | Path,
) -> RecoveryBundleVerification:
    source = Path(bundle_path).resolve()
    sidecar = source.with_suffix(".sha256")
    if not source.is_file() or not sidecar.is_file():
        return RecoveryBundleVerification(
            False, False, "missing", False, False, 0, (), ()
        )
    recorded_parts = sidecar.read_text(encoding="utf-8").split()
    recorded = recorded_parts[0] if recorded_parts else ""
    hash_matches = recorded == sha256_file(source)
    if not hash_matches:
        return RecoveryBundleVerification(
            False, False, "not_checked", False, False, 0, (), ()
        )

    missing: list[str] = []
    mismatched: list[str] = []
    integrity = "not_checked"
    database_hash_matches = False
    counts_match = False
    file_count = 0
    try:
        with zipfile.ZipFile(source) as archive:
            manifest = _read_bundle_manifest(archive)
            database = manifest["database"]
            expected_names = {"manifest.json", str(database["archive_path"])}
            file_entries = manifest.get("files", [])
            file_count = len(file_entries)
            expected_names.update(str(item["archive_path"]) for item in file_entries)
            actual_names = set(archive.namelist())
            missing.extend(sorted(expected_names - actual_names))
            mismatched.extend(
                f"unexpected:{name}" for name in sorted(actual_names - expected_names)
            )

            database_bytes = _entry_bytes(archive, str(database["archive_path"]))
            database_hash_matches = (
                hashlib.sha256(database_bytes).hexdigest() == database["sha256"]
                and len(database_bytes) == int(database["size"])
            )
            if not database_hash_matches:
                mismatched.append(str(database["archive_path"]))
            with tempfile.TemporaryDirectory(prefix="company-verify-") as temp_name:
                database_path = Path(temp_name) / "ledger.sqlite3"
                database_path.write_bytes(database_bytes)
                connection = sqlite3.connect(
                    f"file:{database_path}?mode=ro", uri=True
                )
                try:
                    integrity = str(
                        connection.execute("PRAGMA integrity_check").fetchone()[0]
                    )
                    counts_match = (
                        table_counts(connection) == database["table_counts"]
                    )
                finally:
                    connection.close()

            for item in file_entries:
                name = str(item["archive_path"])
                if name not in actual_names:
                    continue
                content = _entry_bytes(archive, name)
                if (
                    hashlib.sha256(content).hexdigest() != item["sha256"]
                    or len(content) != int(item["size"])
                ):
                    mismatched.append(name)
    except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
        integrity = "bundle_error"
        mismatched.append("bundle")

    ok = (
        hash_matches
        and database_hash_matches
        and integrity == "ok"
        and counts_match
        and not missing
        and not mismatched
    )
    return RecoveryBundleVerification(
        ok,
        hash_matches,
        integrity,
        database_hash_matches,
        counts_match,
        file_count,
        tuple(missing),
        tuple(mismatched),
    )


def restore_recovery_bundle(
    bundle_path: str | Path,
    *,
    new_db_path: str | Path,
    new_root: str | Path,
) -> RecoveryRestoreResult:
    """Restore a verified bundle into new, otherwise-empty locations."""

    source = Path(bundle_path).resolve()
    target_db = Path(new_db_path).resolve()
    target_root = Path(new_root).resolve()
    checked = verify_recovery_bundle(source)
    if not checked.ok:
        raise RuntimeError("recovery bundle verification failed; restore aborted")
    if target_db.exists():
        raise FileExistsError(f"restore database target already exists: {target_db}")
    if target_root.exists() and any(target_root.iterdir()):
        raise FileExistsError(f"restore root target is not empty: {target_root}")

    target_db.parent.mkdir(parents=True, exist_ok=True)
    target_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="company-restore-") as temp_name:
        staging = Path(temp_name)
        staging_root = staging / "root"
        staging_root.mkdir()
        staging_db = staging / "ledger.sqlite3"
        with zipfile.ZipFile(source) as archive:
            manifest = _read_bundle_manifest(archive)
            staging_db.write_bytes(
                _entry_bytes(archive, str(manifest["database"]["archive_path"]))
            )
            for item in manifest["files"]:
                relative = Path(str(item["path"]).replace("/", os.sep))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError(f"unsafe restored runtime path: {item['path']}")
                target = staging_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(_entry_bytes(archive, str(item["archive_path"])))

        created_root = False
        created_db = False
        try:
            if target_root.exists():
                target_root.rmdir()
            shutil.move(str(staging_root), str(target_root))
            created_root = True
            os.replace(staging_db, target_db)
            created_db = True
        except BaseException:
            if created_db and target_db.exists():
                target_db.unlink()
            if created_root and target_root.exists():
                shutil.rmtree(target_root)
            raise

    connection = sqlite3.connect(f"file:{target_db}?mode=ro", uri=True)
    try:
        counts = table_counts(connection)
    finally:
        connection.close()
    return RecoveryRestoreResult(target_db, target_root, counts, checked.file_count)
