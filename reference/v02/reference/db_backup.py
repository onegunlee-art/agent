"""db_backup.py — canonical SQLite 원장 백업·복원 (WorkOrder G).

규칙
- 실행 중인 DB 파일을 그대로 복사하거나 OneDrive에 동기화하지 않는다 (WAL 손상 위험).
- sqlite3 온라인 백업 API로 일관된 스냅샷을 만든 뒤, sha256과 함께 저장한다.
- 복원은 "새 경로에 복원 → integrity_check → 핵심 표 행 수 비교" 까지 통과해야 성공이다.
- 백업 사본(.sqlite3 + .sha256)만 다른 위치(외장 디스크, 동기화 폴더)로 복제한다.

사용
  python db_backup.py backup  --db C:/company-data/ledger.sqlite3 --dir C:/company-backups
  python db_backup.py verify  --backup C:/company-backups/ledger-20260925-101500.sqlite3
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BackupResult:
    path: Path
    sha256: str
    tables: dict[str, int]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def backup(db_path: Path, backup_dir: Path, stem: str = "ledger") -> BackupResult:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)                       # 온라인 백업: 쓰기와 충돌하지 않는 일관 스냅샷
        counts = _table_counts(dst)
    finally:
        dst.close()
        src.close()
    digest = _sha256(target)
    target.with_suffix(".sha256").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    return BackupResult(target, digest, counts)


def verify(backup_path: Path, expected_counts: dict[str, int] | None = None) -> bool:
    """해시 일치 + integrity_check + (선택) 표 행 수 일치."""
    recorded = backup_path.with_suffix(".sha256").read_text(encoding="utf-8").split()[0]
    if recorded != _sha256(backup_path):
        return False
    conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return False
        if expected_counts is not None and _table_counts(conn) != expected_counts:
            return False
    finally:
        conn.close()
    return True


def restore(backup_path: Path, new_db_path: Path) -> dict[str, int]:
    """백업을 새 경로로 복원(온라인 백업 API 역방향)하고 표 행 수를 돌려준다."""
    if not verify(backup_path):
        raise RuntimeError("백업 검증 실패 — 복원 중단")
    src = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    dst = sqlite3.connect(new_db_path)
    try:
        src.backup(dst)
        return _table_counts(dst)
    finally:
        dst.close()
        src.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--db", required=True)
    b.add_argument("--dir", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--backup", required=True)
    r = sub.add_parser("restore")
    r.add_argument("--backup", required=True)
    r.add_argument("--to", required=True)
    a = p.parse_args(argv)
    if a.cmd == "backup":
        res = backup(Path(a.db), Path(a.dir))
        print(res.path, res.sha256, res.tables)
    elif a.cmd == "verify":
        ok = verify(Path(a.backup))
        print("OK" if ok else "FAILED")
        return 0 if ok else 1
    else:
        print(restore(Path(a.backup), Path(a.to)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
