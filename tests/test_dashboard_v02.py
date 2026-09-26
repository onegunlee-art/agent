from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from company_os.application import CompanyOS
from company_os.dashboard import create_dashboard_server, read_dashboard
from company_os.fakes import FakeExecutor
from company_os.model_executor import ExecutorOutcome

from .helpers import build_venture


def _post(url: str, payload: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.read().decode("utf-8")


def test_dashboard_reads_ledger_in_read_only_mode(tmp_path: Path) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "dashboard-read")
        run = company.execute_work_order(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="dashboard-read-run",
        )
        snapshot = read_dashboard(company.db_path)

    item = next(item for item in snapshot["work_orders"] if item["id"] == work_order.id)
    assert item["status"] == "VERIFIED"
    assert item["run_status"] == run.status
    assert snapshot["read_only"] is True


def test_dashboard_preserves_rejected_attempts_and_explains_approval_event(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "dashboard-run-history")
        rejected = ExecutorOutcome(
            ok=True,
            label="DONE",
            cost_usd=None,
            cost_unknown=True,
            duration_seconds=195.45,
            usage={"input_tokens": 355_509, "output_tokens": 3_815},
            usage_status="EXCEEDED",
            usage_total_tokens=359_324,
        )
        company.record_model_execution(
            work_order.id,
            rejected,
            idempotency_key="dashboard-rejected-run",
        )
        accepted = ExecutorOutcome(
            ok=True,
            label="DONE",
            cost_usd=None,
            cost_unknown=True,
            duration_seconds=172.084,
            usage={"input_tokens": 210_000, "output_tokens": 1_901},
            usage_status="WITHIN_LIMIT",
            usage_total_tokens=211_901,
        )
        company.record_model_execution(
            work_order.id,
            accepted,
            idempotency_key="dashboard-accepted-run",
        )
        db_path = company.db_path

    snapshot = read_dashboard(db_path)
    item = next(item for item in snapshot["work_orders"] if item["id"] == work_order.id)
    assert [run["outcome"] for run in item["runs"]] == [
        "DONE",
        "USAGE_LIMIT_EXCEEDED",
    ]
    assert item["runs"][1]["usage_total_tokens"] == 359_324

    server = create_dashboard_server(tmp_path, db_path, port=0)
    _, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            page = response.read().decode("utf-8")
        assert "USAGE_LIMIT_EXCEEDED" in page
        assert "359,324" in page
        assert "승인 Event" in page
        assert "원장 상태를 직접 덮어쓰지 않습니다" in page
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_local_server_approves_and_creates_linked_revision(
    tmp_path: Path,
) -> None:
    with CompanyOS(tmp_path) as company:
        _, _, _, work_order = build_venture(company, "dashboard-actions")
        company.execute_work_order(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="dashboard-actions-run",
        )
        db_path = company.db_path

    server = create_dashboard_server(tmp_path, db_path, port=0)
    host, port = server.server_address
    assert host == "127.0.0.1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            page = response.read().decode("utf-8")
        assert work_order.id in page and "승인" in page

        status, body = _post(
            f"http://127.0.0.1:{port}/approve",
            {"csrf": server.csrf_token, "work_order_id": work_order.id},
        )
        assert status == 200 and json.loads(body)["status"] == "APPROVED"

        status, body = _post(
            f"http://127.0.0.1:{port}/request-change",
            {
                "csrf": server.csrf_token,
                "work_order_id": work_order.id,
                "request": "답변을 세 줄로 줄여 주세요.",
            },
        )
        result = json.loads(body)
        assert status == 200 and result["status"] == "CREATED"

        with CompanyOS(tmp_path, db_path=db_path) as company:
            created = company.store.get_row("work_orders", result["work_order_id"])
            specification = json.loads(created["specification_json"])
            assert created["status"] == "DRAFT"
            assert specification["parent_work_order_id"] == work_order.id
            event_types = {event["event_type"] for event in company.events()}
            assert "CEO_WORK_ORDER_APPROVED" in event_types
            assert "WORK_ORDER_REVISION_REQUESTED" in event_types
    finally:
        server.shutdown()
        server.server_close()
