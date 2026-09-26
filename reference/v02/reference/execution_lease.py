"""execution_lease.py — WorkOrder 실행 lease / fencing token 참조 구현 (WorkOrder A·B).

목적
----
1. 실행은 SQLite 트랜잭션 **밖**에서 돌리고, 청구(claim)와 확정(finalize)만 짧은
   BEGIN IMMEDIATE 트랜잭션으로 처리한다.
2. company stop 검사를 청구 트랜잭션 **안**에 넣어 경쟁 조건을 없앤다.
   - stop이 먼저 커밋되면 새 청구는 거부된다.
   - 청구가 먼저 커밋되면 이미 시작된 실행은 끝까지 갈 수 있다.
3. 청구마다 fence_token을 1 올린다. 확정 시 토큰이 다르면 "오래된 실행의 늦은 결과"이므로
   원장 확정을 거부한다 (StaleExecution).
4. lease_expires_at을 넘긴 EXECUTING 청구는 reclaim_expired()로 회수한다.
   - WORKSPACE_ONLY → READY (자동 재시도 가능)
   - EXTERNAL       → MANUAL_RECOVERY_REQUIRED (자동 재시도 금지)
5. 실패한 실행도 execution_run 행으로 남긴다 (비용·재시도·실패율 계산용).

이식 지침
---------
이 파일은 기존 application.py 스키마를 대체하지 않는다. 표 이름·컬럼을 기존
WorkOrder / Run / Event에 맞춰 옮기되, tests/test_execution_lease.py가 규정하는
**동작**은 바꾸지 않는다. stdlib만 사용한다.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

# ---------------------------------------------------------------- 상태/상수
READY = "READY"
EXECUTING = "EXECUTING"
DONE = "DONE"
FAILED = "FAILED"
MANUAL_RECOVERY_REQUIRED = "MANUAL_RECOVERY_REQUIRED"

WORKSPACE_ONLY = "WORKSPACE_ONLY"
EXTERNAL = "EXTERNAL"

SCHEMA = """
CREATE TABLE IF NOT EXISTS company_state(
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR IGNORE INTO company_state(key, value) VALUES ('stopped', '0');

CREATE TABLE IF NOT EXISTS work_order_exec(
  work_order_id      TEXT PRIMARY KEY,
  status             TEXT NOT NULL,
  fence_token        INTEGER NOT NULL DEFAULT 0,
  execution_id       TEXT,
  lease_expires_at   REAL,
  time_limit_seconds INTEGER NOT NULL,
  cost_limit_usd     REAL NOT NULL,
  side_effect_class  TEXT NOT NULL
      CHECK (side_effect_class IN ('WORKSPACE_ONLY', 'EXTERNAL'))
);

CREATE TABLE IF NOT EXISTS execution_run(
  execution_id  TEXT PRIMARY KEY,
  work_order_id TEXT NOT NULL,
  fence_token   INTEGER NOT NULL,
  started_at    REAL NOT NULL,
  finished_at   REAL,
  outcome       TEXT,
  cost_usd      REAL,
  error         TEXT
);

CREATE TABLE IF NOT EXISTS execution_event(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  at            REAL NOT NULL,
  work_order_id TEXT,
  execution_id  TEXT,
  type          TEXT NOT NULL,
  detail        TEXT
);
"""


# ---------------------------------------------------------------- 예외/자료형
class LeaseError(Exception):
    """lease 관련 기본 예외."""


class CompanyStopped(LeaseError):
    """회사가 정지된 뒤 들어온 청구."""


class NotClaimable(LeaseError):
    """READY 상태가 아니거나 존재하지 않는 WorkOrder."""


class StaleExecution(LeaseError):
    """fence_token 또는 execution_id가 현재 청구와 다른 늦은 결과."""


@dataclass(frozen=True)
class Lease:
    work_order_id: str
    execution_id: str
    fence_token: int
    lease_expires_at: float
    cost_limit_usd: float
    side_effect_class: str


@dataclass
class ExecResult:
    """실행자가 돌려주는 결과. label은 실패 유형(ERROR, TIMEOUT, MISSING_ARTIFACT ...)."""

    ok: bool
    cost_usd: float = 0.0
    error: Optional[str] = None
    label: Optional[str] = None


# ---------------------------------------------------------------- 연결/도우미
def connect(path: str = ":memory:") -> sqlite3.Connection:
    """isolation_level=None: 트랜잭션 경계를 우리가 직접 BEGIN/COMMIT으로 통제한다."""
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[None]:
    """짧은 쓰기 트랜잭션. 예외면 ROLLBACK, 정상이면 COMMIT."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _event(conn, at, work_order_id, execution_id, type_, detail=None) -> None:
    conn.execute(
        "INSERT INTO execution_event(at, work_order_id, execution_id, type, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        (at, work_order_id, execution_id, type_,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None),
    )


def _is_stopped(conn) -> bool:
    row = conn.execute("SELECT value FROM company_state WHERE key='stopped'").fetchone()
    return bool(row and row["value"] == "1")


# ---------------------------------------------------------------- 회사 정지/재개
def stop_company(conn: sqlite3.Connection, now: float) -> None:
    with immediate(conn):
        conn.execute("UPDATE company_state SET value='1' WHERE key='stopped'")
        _event(conn, now, None, None, "COMPANY_STOPPED")


def resume_company(conn: sqlite3.Connection, now: float) -> None:
    with immediate(conn):
        conn.execute("UPDATE company_state SET value='0' WHERE key='stopped'")
        _event(conn, now, None, None, "COMPANY_RESUMED")


# ---------------------------------------------------------------- WorkOrder 등록
def register_work_order(
    conn: sqlite3.Connection,
    work_order_id: str,
    time_limit_seconds: int,
    cost_limit_usd: float,
    side_effect_class: str = WORKSPACE_ONLY,
) -> None:
    conn.execute(
        "INSERT INTO work_order_exec(work_order_id, status, time_limit_seconds, "
        "cost_limit_usd, side_effect_class) VALUES (?, ?, ?, ?, ?)",
        (work_order_id, READY, time_limit_seconds, cost_limit_usd, side_effect_class),
    )


def get_status(conn: sqlite3.Connection, work_order_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT status FROM work_order_exec WHERE work_order_id=?", (work_order_id,)
    ).fetchone()
    return row["status"] if row else None


# ---------------------------------------------------------------- 청구
def claim(conn: sqlite3.Connection, work_order_id: str, now: float) -> Lease:
    """READY → EXECUTING. stop 검사와 청구가 같은 BEGIN IMMEDIATE 안에서 일어난다."""
    rejection: Optional[LeaseError] = None
    lease: Optional[Lease] = None

    with immediate(conn):
        if _is_stopped(conn):
            rejection = CompanyStopped(work_order_id)
        else:
            row = conn.execute(
                "SELECT * FROM work_order_exec WHERE work_order_id=?", (work_order_id,)
            ).fetchone()
            if row is None or row["status"] != READY:
                rejection = NotClaimable(
                    f"{work_order_id}: status={row['status'] if row else None}"
                )
            else:
                fence = row["fence_token"] + 1
                execution_id = uuid.uuid4().hex
                expires = now + row["time_limit_seconds"]
                conn.execute(
                    "UPDATE work_order_exec SET status=?, fence_token=?, execution_id=?, "
                    "lease_expires_at=? WHERE work_order_id=?",
                    (EXECUTING, fence, execution_id, expires, work_order_id),
                )
                conn.execute(
                    "INSERT INTO execution_run(execution_id, work_order_id, fence_token, "
                    "started_at) VALUES (?, ?, ?, ?)",
                    (execution_id, work_order_id, fence, now),
                )
                lease = Lease(
                    work_order_id=work_order_id,
                    execution_id=execution_id,
                    fence_token=fence,
                    lease_expires_at=expires,
                    cost_limit_usd=row["cost_limit_usd"],
                    side_effect_class=row["side_effect_class"],
                )
                _event(conn, now, work_order_id, execution_id, "EXECUTION_CLAIMED",
                       {"fence_token": fence, "lease_expires_at": expires})

    if rejection is not None:
        with immediate(conn):
            _event(conn, now, work_order_id, None, "CLAIM_REJECTED",
                   {"reason": type(rejection).__name__})
        raise rejection
    assert lease is not None
    return lease


# ---------------------------------------------------------------- 확정
def finalize(conn: sqlite3.Connection, lease: Lease, result: ExecResult, now: float) -> str:
    """실행 결과를 원장에 확정한다. 반환값은 outcome 라벨.

    거부 규칙 (순서대로):
      1. fence_token/execution_id 불일치  → StaleExecution (원장 변경 없음, 이벤트만 기록)
      2. lease 만료 후 도착한 결과         → outcome=EXPIRED, 산출물 확정 안 함
      3. 비용 상한 초과                    → outcome=COST_LIMIT_EXCEEDED, status=FAILED
      4. 실행자 실패                       → outcome=result.label or ERROR,
                                             WORKSPACE_ONLY면 READY로 복구, EXTERNAL이면 수동 복구
      5. 정상                              → outcome=DONE
    """
    stale = False
    outcome = ""
    with immediate(conn):
        row = conn.execute(
            "SELECT * FROM work_order_exec WHERE work_order_id=?", (lease.work_order_id,)
        ).fetchone()
        if (
            row is None
            or row["execution_id"] != lease.execution_id
            or row["fence_token"] != lease.fence_token
        ):
            stale = True
            _event(conn, now, lease.work_order_id, lease.execution_id,
                   "STALE_RESULT_REJECTED",
                   {"lease_fence": lease.fence_token,
                    "current_fence": row["fence_token"] if row else None})
        else:
            recover_status = READY if lease.side_effect_class == WORKSPACE_ONLY \
                else MANUAL_RECOVERY_REQUIRED
            if now > lease.lease_expires_at:
                outcome, status = "EXPIRED", recover_status
            elif result.cost_usd > lease.cost_limit_usd:
                outcome, status = "COST_LIMIT_EXCEEDED", FAILED
            elif result.ok:
                outcome, status = "DONE", DONE
            else:
                outcome, status = (result.label or "ERROR"), recover_status

            conn.execute(
                "UPDATE execution_run SET finished_at=?, outcome=?, cost_usd=?, error=? "
                "WHERE execution_id=?",
                (now, outcome, result.cost_usd, result.error, lease.execution_id),
            )
            # fence를 한 번 더 올려 같은 lease로 두 번 확정하는 것도 막는다.
            conn.execute(
                "UPDATE work_order_exec SET status=?, execution_id=NULL, lease_expires_at=NULL, "
                "fence_token=fence_token+1 WHERE work_order_id=?",
                (status, lease.work_order_id),
            )
            _event(conn, now, lease.work_order_id, lease.execution_id, "EXECUTION_FINALIZED",
                   {"outcome": outcome, "status": status, "cost_usd": result.cost_usd})

    if stale:
        raise StaleExecution(f"{lease.work_order_id}/{lease.execution_id}")
    return outcome


# ---------------------------------------------------------------- 만료 회수
def reclaim_expired(conn: sqlite3.Connection, now: float) -> list[dict]:
    """프로세스 시작 시(그리고 주기적으로) 호출. lease가 만료된 EXECUTING 청구를 회수한다."""
    reclaimed: list[dict] = []
    with immediate(conn):
        rows = conn.execute(
            "SELECT * FROM work_order_exec WHERE status=? AND lease_expires_at < ?",
            (EXECUTING, now),
        ).fetchall()
        for row in rows:
            new_status = READY if row["side_effect_class"] == WORKSPACE_ONLY \
                else MANUAL_RECOVERY_REQUIRED
            conn.execute(
                "UPDATE work_order_exec SET status=?, fence_token=fence_token+1, "
                "execution_id=NULL, lease_expires_at=NULL WHERE work_order_id=?",
                (new_status, row["work_order_id"]),
            )
            conn.execute(
                "UPDATE execution_run SET finished_at=?, outcome='EXPIRED', "
                "error='lease expired; reclaimed' WHERE execution_id=? AND finished_at IS NULL",
                (now, row["execution_id"]),
            )
            _event(conn, now, row["work_order_id"], row["execution_id"], "LEASE_RECLAIMED",
                   {"new_status": new_status})
            reclaimed.append({"work_order_id": row["work_order_id"],
                              "execution_id": row["execution_id"],
                              "new_status": new_status})
    return reclaimed


# ---------------------------------------------------------------- 실행 경계
def run_with_lease(
    conn: sqlite3.Connection,
    work_order_id: str,
    executor: Callable[[Lease], ExecResult],
    now_fn: Callable[[], float] = time.time,
) -> str:
    """claim → (트랜잭션 밖에서) executor → finalize. 실행자 예외도 Run/Event로 남긴다."""
    lease = claim(conn, work_order_id, now_fn())
    try:
        result = executor(lease)
    except Exception as exc:  # noqa: BLE001 - 실행자 오류는 모두 기록 대상
        result = ExecResult(ok=False, error=repr(exc), label="ERROR")
    return finalize(conn, lease, result, now_fn())
