"""Minimal localhost-only WorkOrder dashboard for V0.2."""

from __future__ import annotations

import html
import json
import secrets
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .application import CompanyOS
from .errors import NotFoundError, ValidationError
from .storage import new_id, utc_now


def _read_only_connection(db_path: str | Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def read_dashboard(db_path: str | Path) -> dict[str, Any]:
    connection = _read_only_connection(db_path)
    try:
        rows = connection.execute(
            """
            SELECT w.id, w.title, w.status, w.venture_id,
                   r.id AS run_id, r.status AS run_status, r.outcome AS run_outcome,
                   r.cost_usd, r.duration_seconds,
                   rv.id AS review_id, rv.status AS review_status,
                   rv.source_commit, rv.source_tree_sha256
            FROM work_orders AS w
            LEFT JOIN runs AS r ON r.id = (
                SELECT id FROM runs
                WHERE work_order_id = w.id
                ORDER BY created_at DESC, id DESC LIMIT 1
            )
            LEFT JOIN reviews AS rv ON rv.id = (
                SELECT id FROM reviews
                WHERE work_order_id = w.id
                ORDER BY created_at DESC, id DESC LIMIT 1
            )
            ORDER BY w.created_at DESC, w.id DESC
            """
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            rubric = connection.execute(
                """
                SELECT payload_json FROM evidence
                WHERE work_order_id = ? AND kind = 'RUBRIC_REPORT'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
            item["rubric"] = json.loads(rubric["payload_json"]) if rubric else None
            items.append(item)
        return {"read_only": True, "work_orders": items}
    finally:
        connection.close()


def _work_binding(connection: sqlite3.Connection, work_order_id: str) -> dict[str, Any]:
    work = connection.execute(
        "SELECT id, venture_id, verifier_sha256 FROM work_orders WHERE id = ?",
        (work_order_id,),
    ).fetchone()
    if work is None:
        raise NotFoundError(f"WorkOrder not found: {work_order_id}")
    run = connection.execute(
        """
        SELECT id, status, execution_id, fence_token FROM runs
        WHERE work_order_id = ? ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (work_order_id,),
    ).fetchone()
    review = connection.execute(
        """
        SELECT id, source_commit, source_tree_sha256 FROM reviews
        WHERE work_order_id = ? ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (work_order_id,),
    ).fetchone()
    artifacts = connection.execute(
        "SELECT id, sha256 FROM artifacts WHERE work_order_id = ? ORDER BY id",
        (work_order_id,),
    ).fetchall()
    return {
        "work_order_id": work_order_id,
        "verifier_sha256": work["verifier_sha256"],
        "run": dict(run) if run else None,
        "review": dict(review) if review else None,
        "artifacts": [dict(row) for row in artifacts],
    }


def record_ceo_approval(company: CompanyOS, work_order_id: str) -> dict[str, str]:
    now = utc_now()
    decision_id = new_id("decision")
    approval_id = new_id("approval")
    with company.store.transaction() as connection:
        work = connection.execute(
            "SELECT venture_id FROM work_orders WHERE id = ?", (work_order_id,)
        ).fetchone()
        if work is None:
            raise NotFoundError(f"WorkOrder not found: {work_order_id}")
        binding = _work_binding(connection, work_order_id)
        company.store.insert_row(
            "decisions",
            {
                "id": decision_id,
                "venture_id": work["venture_id"],
                "work_order_id": work_order_id,
                "status": "APPROVED",
                "payload_json": json.dumps(
                    {"kind": "WORK_ORDER_ACCEPTANCE", "binding": binding},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "created_at": now,
                "updated_at": now,
            },
            connection=connection,
        )
        company.store.insert_row(
            "approvals",
            {
                "id": approval_id,
                "contract_id": None,
                "decision_id": decision_id,
                "status": "APPROVED",
                "actor": "CEO",
                "payload_json": json.dumps(binding, ensure_ascii=False, sort_keys=True),
                "created_at": now,
            },
            connection=connection,
        )
        company.store.append_event(
            "CEO_WORK_ORDER_APPROVED",
            aggregate_type="WorkOrder",
            aggregate_id=work_order_id,
            venture_id=work["venture_id"],
            payload={
                "decision_id": decision_id,
                "approval_id": approval_id,
                "binding": binding,
            },
            connection=connection,
        )
    return {"status": "APPROVED", "approval_id": approval_id}


def request_revision(
    company: CompanyOS,
    work_order_id: str,
    request_text: str,
) -> dict[str, str]:
    request_text = request_text.strip()
    if not request_text:
        raise ValidationError("revision request must not be empty")
    if len(request_text) > 2_000:
        raise ValidationError("revision request exceeds 2000 characters")
    new_work_order_id = new_id("work_order")
    now = utc_now()
    with company.store.transaction() as connection:
        original = connection.execute(
            "SELECT * FROM work_orders WHERE id = ?", (work_order_id,)
        ).fetchone()
        if original is None:
            raise NotFoundError(f"WorkOrder not found: {work_order_id}")
        specification = json.loads(original["specification_json"])
        specification.update(
            {
                "parent_work_order_id": work_order_id,
                "revision_request": request_text,
                "source_binding": _work_binding(connection, work_order_id),
            }
        )
        company.store.insert_row(
            "work_orders",
            {
                "id": new_work_order_id,
                "venture_id": original["venture_id"],
                "experiment_id": original["experiment_id"],
                "title": f"Revision: {original['title']}",
                "status": "DRAFT",
                "specification_json": json.dumps(
                    specification, ensure_ascii=False, sort_keys=True
                ),
                "verifier_path": original["verifier_path"],
                "verifier_sha256": original["verifier_sha256"],
                "execution_id": None,
                "fence_token": 0,
                "lease_expires_at": None,
                "time_limit_seconds": original["time_limit_seconds"],
                "cost_limit_usd": original["cost_limit_usd"],
                "model_call_limit": original["model_call_limit"],
                "token_limit": original["token_limit"],
                "side_effect_class": original["side_effect_class"],
                "created_at": now,
                "updated_at": now,
            },
            connection=connection,
        )
        company.store.append_event(
            "WORK_ORDER_REVISION_REQUESTED",
            aggregate_type="WorkOrder",
            aggregate_id=new_work_order_id,
            venture_id=original["venture_id"],
            payload={
                "parent_work_order_id": work_order_id,
                "request": request_text,
            },
            connection=connection,
        )
    return {"status": "CREATED", "work_order_id": new_work_order_id}


def _render_page(snapshot: dict[str, Any], csrf_token: str, preview_url: str) -> str:
    cards: list[str] = []
    for item in snapshot["work_orders"]:
        rubric = item.get("rubric") or {}
        cards.append(
            "<article>"
            f"<h2>{html.escape(item['title'])}</h2>"
            f"<code>{html.escape(item['id'])}</code>"
            f"<p>상태: <strong>{html.escape(item['status'])}</strong></p>"
            f"<p>최근 실행: {html.escape(str(item.get('run_status') or '없음'))} / "
            f"비용: {html.escape(str(item.get('cost_usd')))} USD / "
            f"시간: {html.escape(str(item.get('duration_seconds')))}초</p>"
            f"<p>평가: {html.escape(str(rubric.get('verdict', '없음')))} "
            f"({html.escape(str(rubric.get('score', '-')))}) · "
            f"Claude: {html.escape(str(item.get('review_status') or '미요청'))}</p>"
            f'<p><a href="{html.escape(preview_url, quote=True)}" target="_blank">챗봇 미리보기 열기</a></p>'
            f'<form method="post" action="/approve"><input type="hidden" name="csrf" value="{csrf_token}">'
            f'<input type="hidden" name="work_order_id" value="{html.escape(item["id"], quote=True)}">'
            '<button type="submit">승인</button></form>'
            f'<form method="post" action="/request-change"><input type="hidden" name="csrf" value="{csrf_token}">'
            f'<input type="hidden" name="work_order_id" value="{html.escape(item["id"], quote=True)}">'
            '<input name="request" maxlength="2000" placeholder="수정 요청"><button type="submit">수정 요청</button></form>'
            "</article>"
        )
    return """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AI Company OS</title>
<style>body{font-family:system-ui;max-width:960px;margin:2rem auto;padding:0 1rem;background:#f5f6f8}
article{background:white;border:1px solid #ddd;border-radius:12px;padding:1rem;margin:1rem 0}
form{display:flex;gap:.5rem;margin-top:.6rem}input[name=request]{flex:1;padding:.5rem}</style></head>
<body><h1>AI Company OS 업무 현황</h1><p>원장 조회는 SQLite read-only 연결입니다.</p>""" + "".join(cards) + "</body></html>"


class DashboardServer(ThreadingHTTPServer):
    csrf_token: str


def create_dashboard_server(
    root: str | Path,
    db_path: str | Path,
    *,
    port: int = 8780,
    preview_url: str = "http://127.0.0.1:8765/",
) -> DashboardServer:
    root_path = Path(root).resolve()
    database = Path(db_path).resolve()
    csrf_token = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(200, b'{"ok":true}', "application/json")
                return
            if self.path != "/":
                self._send(404, b"not found", "text/plain")
                return
            page = _render_page(read_dashboard(database), csrf_token, preview_url)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 16_384)
                values = urllib.parse.parse_qs(
                    self.rfile.read(length).decode("utf-8"), keep_blank_values=True
                )
                if values.get("csrf", [""])[0] != csrf_token:
                    raise ValidationError("invalid CSRF token")
                work_order_id = values.get("work_order_id", [""])[0]
                with CompanyOS(root_path, db_path=database) as company:
                    if self.path == "/approve":
                        result = record_ceo_approval(company, work_order_id)
                    elif self.path == "/request-change":
                        result = request_revision(
                            company,
                            work_order_id,
                            values.get("request", [""])[0],
                        )
                    else:
                        self._send(404, b"not found", "text/plain")
                        return
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except (NotFoundError, ValidationError, ValueError) as exc:
                body = json.dumps(
                    {"error": type(exc).__name__, "message": str(exc)},
                    ensure_ascii=False,
                ).encode("utf-8")
                self._send(400, body, "application/json; charset=utf-8")

        def log_message(self, *_: object) -> None:
            return

    server = DashboardServer(("127.0.0.1", port), Handler)
    server.csrf_token = csrf_token
    return server


def serve_dashboard(
    root: str | Path,
    db_path: str | Path,
    *,
    port: int = 8780,
    preview_url: str = "http://127.0.0.1:8765/",
) -> None:
    server = create_dashboard_server(root, db_path, port=port, preview_url=preview_url)
    print(f"dashboard=http://127.0.0.1:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
