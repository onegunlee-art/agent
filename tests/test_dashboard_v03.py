from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from company_os.application import CompanyOS
from company_os.dashboard import create_dashboard_server, read_dashboard
from company_os.ledger_backup import backup
from company_os.model_executor import ExecutorOutcome

from .helpers import build_venture


def _model_outcome(
    *,
    tokens: int,
    duration: float,
    cost_usd: float,
    usage_status: str,
) -> ExecutorOutcome:
    return ExecutorOutcome(
        ok=True,
        label="DONE",
        cost_usd=cost_usd,
        cost_unknown=False,
        duration_seconds=duration,
        usage={"input_tokens": tokens - 10, "output_tokens": 10},
        usage_status=usage_status,
        usage_total_tokens=tokens,
    )


def test_dashboard_prioritizes_ceo_attention_and_separates_request_cost(
    tmp_path: Path,
) -> None:
    company_root = tmp_path / "company"
    backup_dir = tmp_path / "backups"
    with CompanyOS(company_root) as company:
        _, _, _, pending = build_venture(company, "dashboard-v03-pending")
        company.record_model_execution(
            pending.id,
            _model_outcome(
                tokens=100,
                duration=5.0,
                cost_usd=0.1,
                usage_status="WITHIN_LIMIT",
            ),
            idempotency_key="dashboard-v03-pending-run",
        )

        _, _, _, approved = build_venture(company, "dashboard-v03-approved")
        company.record_model_execution(
            approved.id,
            _model_outcome(
                tokens=400,
                duration=20.0,
                cost_usd=0.9,
                usage_status="EXCEEDED",
            ),
            idempotency_key="dashboard-v03-approved-rejected",
        )
        accepted = company.record_model_execution(
            approved.id,
            _model_outcome(
                tokens=200,
                duration=10.0,
                cost_usd=0.4,
                usage_status="WITHIN_LIMIT",
            ),
            idempotency_key="dashboard-v03-approved-accepted",
        )
        with company.store.transaction() as connection:
            company.store.append_event(
                "CEO_WORK_ORDER_APPROVED",
                aggregate_type="WorkOrder",
                aggregate_id=approved.id,
                venture_id=approved.venture_id,
                payload={"source": "acceptance-test"},
                connection=connection,
            )
        backup(company.db_path, backup_dir, timestamp="20260927-010203")
        db_path = company.db_path

    snapshot = read_dashboard(db_path, backup_dir=backup_dir)

    assert snapshot["read_only"] is True
    assert snapshot["work_orders"][0]["id"] == pending.id
    assert snapshot["work_orders"][0]["attention_kind"] == "APPROVAL_PENDING"
    approved_item = next(
        item for item in snapshot["work_orders"] if item["id"] == approved.id
    )
    assert approved_item["attention_kind"] is None
    assert approved_item["cost_summary"] == {
        "accepted_run_id": accepted.id,
        "accepted_tokens": 200,
        "accepted_duration_seconds": 10.0,
        "accepted_cost_usd": 0.4,
        "request_total_tokens": 600,
        "request_total_duration_seconds": 30.0,
        "request_total_cost_usd": 1.3,
        "request_has_unknown_usd": False,
        "attempt_count": 2,
    }
    assert [run["outcome"] for run in approved_item["runs"]] == [
        "DONE",
        "USAGE_LIMIT_EXCEEDED",
    ]
    assert snapshot["last_backup"]["name"] == "ledger-20260927-010203.sqlite3"
    assert snapshot["last_backup"]["created_at"]


def test_dashboard_page_shows_operating_summary_and_backup_time(
    tmp_path: Path,
) -> None:
    backup_dir = tmp_path / "backups"
    with CompanyOS(tmp_path / "company") as company:
        _, _, _, work_order = build_venture(company, "dashboard-v03-page")
        company.record_model_execution(
            work_order.id,
            _model_outcome(
                tokens=250,
                duration=12.5,
                cost_usd=0.25,
                usage_status="WITHIN_LIMIT",
            ),
            idempotency_key="dashboard-v03-page-run",
        )
        backup(company.db_path, backup_dir, timestamp="20260927-020304")
        db_path = company.db_path

    server = create_dashboard_server(
        tmp_path / "company",
        db_path,
        port=0,
        backup_dir=backup_dir,
    )
    host, port = server.server_address
    assert host == "127.0.0.1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            page = response.read().decode("utf-8")
        assert "CEO 확인 필요" in page
        assert "채택 Run 원가" in page
        assert "요청 전체 원가" in page
        assert "마지막 백업" in page
        assert "ledger-20260927-020304.sqlite3" in page
        assert "승인 Event만 추가" in page
        assert "CLI 명령으로 기록" in page
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_buttons_use_cli_and_append_events_only(tmp_path: Path) -> None:
    company_root = tmp_path / "company"
    with CompanyOS(company_root) as company:
        _, _, _, work_order = build_venture(company, "dashboard-v03-actions")
        company.record_model_execution(
            work_order.id,
            _model_outcome(
                tokens=120,
                duration=4.0,
                cost_usd=0.12,
                usage_status="WITHIN_LIMIT",
            ),
            idempotency_key="dashboard-v03-actions-run",
        )
        db_path = company.db_path
        before = {
            "work_orders": company.store.scalar("SELECT COUNT(*) FROM work_orders"),
            "decisions": company.store.scalar("SELECT COUNT(*) FROM decisions"),
            "approvals": company.store.scalar("SELECT COUNT(*) FROM approvals"),
        }

    server = create_dashboard_server(company_root, db_path, port=0)
    _, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/approve",
            data=(
                f"csrf={server.csrf_token}&work_order_id={work_order.id}"
            ).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            approved = json.loads(response.read().decode("utf-8"))
        assert approved["status"] == "RECORDED"
        assert approved["event_type"] == "CEO_WORK_ORDER_APPROVED"

        body = urllib.parse.urlencode(
            {
                "csrf": server.csrf_token,
                "work_order_id": work_order.id,
                "request": "답변을 세 줄로 줄여 주세요.",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/request-change",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            changed = json.loads(response.read().decode("utf-8"))
        assert changed["status"] == "RECORDED"
        assert changed["event_type"] == "CEO_WORK_ORDER_CHANGE_REQUESTED"
    finally:
        server.shutdown()
        server.server_close()

    with CompanyOS(company_root, db_path=db_path) as company:
        after = {
            "work_orders": company.store.scalar("SELECT COUNT(*) FROM work_orders"),
            "decisions": company.store.scalar("SELECT COUNT(*) FROM decisions"),
            "approvals": company.store.scalar("SELECT COUNT(*) FROM approvals"),
        }
        assert after == before
        actions = [
            event
            for event in company.events()
            if event["event_type"]
            in {"CEO_WORK_ORDER_APPROVED", "CEO_WORK_ORDER_CHANGE_REQUESTED"}
        ]
        assert [event["event_type"] for event in actions[-2:]] == [
            "CEO_WORK_ORDER_APPROVED",
            "CEO_WORK_ORDER_CHANGE_REQUESTED",
        ]
        assert actions[-1]["payload"]["request"] == "답변을 세 줄로 줄여 주세요."
