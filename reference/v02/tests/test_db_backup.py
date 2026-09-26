import sys, tempfile, sqlite3
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1] / "reference"
sys.path.insert(0, str(ROOT))
import execution_lease as L  # noqa: E402
import db_backup as B  # noqa: E402


def _ledger(path: Path):
    conn = L.connect(str(path))
    L.register_work_order(conn, "WO-1", 60, 1.0)
    L.claim(conn, "WO-1", now=100)          # WAL에 쓰기가 남아 있는 상태
    return conn


def test_backup_verify_restore_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "ledger.sqlite3"
        conn = _ledger(db)
        res = B.backup(db, Path(d) / "bk")
        assert res.path.exists() and res.path.with_suffix(".sha256").exists()
        assert res.tables["work_order_exec"] == 1 and res.tables["execution_run"] == 1
        assert B.verify(res.path, expected_counts=res.tables)
        restored = B.restore(res.path, Path(d) / "restored.sqlite3")
        assert restored == res.tables
        r = sqlite3.connect(Path(d) / "restored.sqlite3")
        assert r.execute("SELECT status FROM work_order_exec").fetchone()[0] == "EXECUTING"
        r.close(); conn.close()


def test_tampered_backup_fails_verification():
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "ledger.sqlite3"
        conn = _ledger(db)
        res = B.backup(db, Path(d) / "bk")
        with open(res.path, "r+b") as f:
            f.seek(100); f.write(b"\x00" * 8)
        assert not B.verify(res.path)
        conn.close()
