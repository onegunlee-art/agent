"""WorkOrder A·B 수용 테스트. 이 파일이 규정하는 동작은 이식 후에도 유지되어야 한다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reference"))

import execution_lease as L  # noqa: E402


def _db():
    conn = L.connect(":memory:")
    L.register_work_order(conn, "WO-1", time_limit_seconds=60, cost_limit_usd=1.0)
    return conn


def _events(conn, type_):
    return conn.execute(
        "SELECT * FROM execution_event WHERE type=?", (type_,)).fetchall()


def test_stop_committed_first_rejects_claim():
    conn = _db()
    L.stop_company(conn, now=100)
    try:
        L.claim(conn, "WO-1", now=101)
        assert False, "정지 후 청구는 거부돼야 한다"
    except L.CompanyStopped:
        pass
    assert L.get_status(conn, "WO-1") == L.READY
    assert len(_events(conn, "CLAIM_REJECTED")) == 1


def test_claim_committed_first_can_finish_after_stop():
    conn = _db()
    lease = L.claim(conn, "WO-1", now=100)
    L.stop_company(conn, now=101)
    outcome = L.finalize(conn, lease, L.ExecResult(ok=True, cost_usd=0.1), now=110)
    assert outcome == "DONE"
    assert L.get_status(conn, "WO-1") == L.DONE


def test_same_work_order_cannot_be_claimed_twice_but_different_can_run_in_parallel():
    conn = _db()
    L.register_work_order(conn, "WO-2", time_limit_seconds=60, cost_limit_usd=1.0)
    l1 = L.claim(conn, "WO-1", now=100)
    l2 = L.claim(conn, "WO-2", now=100)
    assert l1.execution_id != l2.execution_id
    try:
        L.claim(conn, "WO-1", now=101)
        assert False
    except L.NotClaimable:
        pass
    assert L.get_status(conn, "WO-1") == L.EXECUTING
    assert L.get_status(conn, "WO-2") == L.EXECUTING


def test_expired_lease_is_reclaimed_and_late_result_is_rejected():
    conn = _db()
    lease = L.claim(conn, "WO-1", now=100)          # 만료 = 160
    reclaimed = L.reclaim_expired(conn, now=200)
    assert [r["work_order_id"] for r in reclaimed] == ["WO-1"]
    assert L.get_status(conn, "WO-1") == L.READY      # WORKSPACE_ONLY → 재시도 가능
    run = conn.execute("SELECT * FROM execution_run WHERE execution_id=?",
                       (lease.execution_id,)).fetchone()
    assert run["outcome"] == "EXPIRED"
    try:
        L.finalize(conn, lease, L.ExecResult(ok=True), now=201)
        assert False, "회수된 lease의 늦은 결과는 확정되면 안 된다"
    except L.StaleExecution:
        pass
    assert L.get_status(conn, "WO-1") == L.READY
    assert len(_events(conn, "STALE_RESULT_REJECTED")) == 1
    # 새 청구는 새 fence를 받는다
    lease2 = L.claim(conn, "WO-1", now=202)
    assert lease2.fence_token > lease.fence_token


def test_external_side_effect_expiry_requires_manual_recovery():
    conn = L.connect(":memory:")
    L.register_work_order(conn, "WO-EXT", 60, 1.0, side_effect_class=L.EXTERNAL)
    L.claim(conn, "WO-EXT", now=100)
    L.reclaim_expired(conn, now=200)
    assert L.get_status(conn, "WO-EXT") == L.MANUAL_RECOVERY_REQUIRED
    try:
        L.claim(conn, "WO-EXT", now=201)
        assert False, "수동 복구 상태는 자동 재시도되면 안 된다"
    except L.NotClaimable:
        pass


def test_result_arriving_after_expiry_is_marked_expired_not_done():
    conn = _db()
    lease = L.claim(conn, "WO-1", now=100)
    outcome = L.finalize(conn, lease, L.ExecResult(ok=True), now=170)  # 만료 160 이후
    assert outcome == "EXPIRED"
    assert L.get_status(conn, "WO-1") == L.READY


def test_cost_limit_exceeded_fails_terminally():
    conn = _db()
    lease = L.claim(conn, "WO-1", now=100)
    outcome = L.finalize(conn, lease, L.ExecResult(ok=True, cost_usd=1.5), now=110)
    assert outcome == "COST_LIMIT_EXCEEDED"
    assert L.get_status(conn, "WO-1") == L.FAILED


def test_executor_exception_is_recorded_as_run_and_state_restored():
    conn = _db()

    def boom(lease):
        raise FileNotFoundError("artifact missing")

    outcome = L.run_with_lease(conn, "WO-1", boom, now_fn=lambda: 100)
    assert outcome == "ERROR"
    assert L.get_status(conn, "WO-1") == L.READY
    run = conn.execute("SELECT * FROM execution_run").fetchone()
    assert run["outcome"] == "ERROR" and "artifact missing" in run["error"]


def test_no_transaction_is_open_while_executor_runs():
    conn = _db()

    def probe(lease):
        assert not conn.in_transaction, "실행 중에는 트랜잭션이 열려 있으면 안 된다"
        return L.ExecResult(ok=True)

    assert L.run_with_lease(conn, "WO-1", probe, now_fn=lambda: 100) == "DONE"


def test_same_lease_cannot_finalize_twice():
    conn = _db()
    lease = L.claim(conn, "WO-1", now=100)
    L.finalize(conn, lease, L.ExecResult(ok=True), now=110)
    try:
        L.finalize(conn, lease, L.ExecResult(ok=True), now=111)
        assert False
    except L.StaleExecution:
        pass
