"""Localhost-only operating dashboard backed by the canonical ledger."""

from __future__ import annotations

import html
from hashlib import sha256
import json
import os
import re
import subprocess
import secrets
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .application import CompanyOS
from .errors import ConflictError, NotFoundError, ValidationError
from .storage import IdempotencyConflict, IdempotencyInProgress


def _read_only_connection(db_path: str | Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _latest_backup(backup_dir: str | Path | None) -> dict[str, Any] | None:
    if backup_dir is None:
        return None
    directory = Path(backup_dir).resolve()
    if not directory.is_dir():
        return None
    candidates = [
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix.casefold() in {".sqlite3", ".zip"}
        and path.with_suffix(".sha256").is_file()
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))
    created_at = datetime.fromtimestamp(
        latest.stat().st_mtime,
        tz=timezone.utc,
    ).astimezone().isoformat()
    return {
        "name": latest.name,
        "created_at": created_at,
        "kind": "RECOVERY_BUNDLE" if latest.suffix.casefold() == ".zip" else "LEDGER",
    }


def _run_tokens(payload: dict[str, Any]) -> int | None:
    total = payload.get("usage_total_tokens")
    if isinstance(total, int) and not isinstance(total, bool):
        return total
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (input_tokens, output_tokens)
    ):
        return int(input_tokens) + int(output_tokens)
    return None


def _cost_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = next(
        (
            run
            for run in runs
            if (run.get("outcome") or run.get("status")) in {"DONE", "PASS"}
            and run.get("usage_status") != "EXCEEDED"
            and run.get("cost_status") != "EXCEEDED"
        ),
        None,
    )
    token_values = [run["usage_total_tokens"] for run in runs]
    durations = [
        float(run["duration_seconds"])
        for run in runs
        if isinstance(run.get("duration_seconds"), (int, float))
        and not isinstance(run.get("duration_seconds"), bool)
    ]
    known_costs = [
        float(run["cost_usd"])
        for run in runs
        if isinstance(run.get("cost_usd"), (int, float))
        and not isinstance(run.get("cost_usd"), bool)
    ]
    return {
        "accepted_run_id": None if accepted is None else accepted["id"],
        "accepted_tokens": (
            None if accepted is None else accepted.get("usage_total_tokens")
        ),
        "accepted_duration_seconds": (
            None if accepted is None else accepted.get("duration_seconds")
        ),
        "accepted_cost_usd": None if accepted is None else accepted.get("cost_usd"),
        "request_total_tokens": sum(
            value for value in token_values if isinstance(value, int)
        ),
        "request_total_duration_seconds": round(sum(durations), 6),
        "request_total_cost_usd": (
            round(sum(known_costs), 6) if known_costs else None
        ),
        "request_has_unknown_usd": len(known_costs) != len(runs),
        "attempt_count": len(runs),
    }


def _request_group_id(run: dict[str, Any]) -> str:
    explicit = run.get("production_request_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    branch = run.get("workspace_branch")
    if isinstance(branch, str) and branch.strip():
        return re.sub(
            r"-(?:retry|final|attempt)(?:-\d+)?$",
            "",
            branch.strip(),
            flags=re.IGNORECASE,
        )
    return "legacy-unclassified"


def _request_groups(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        if not isinstance(run.get("usage_total_tokens"), int) and not run.get(
            "workspace_branch"
        ):
            continue
        group_id = _request_group_id(run)
        grouped.setdefault(group_id, []).append(run)
    return [
        {
            "id": group_id,
            "runs": group_runs,
            "summary": _cost_summary(group_runs),
        }
        for group_id, group_runs in grouped.items()
    ]


def _attention_kind(
    item: dict[str, Any],
    *,
    latest_action: sqlite3.Row | None,
    pending_decisions: int,
) -> str | None:
    summary = item["cost_summary"]
    accepted_run_id = summary["accepted_run_id"]
    if latest_action is not None:
        action_payload = json.loads(latest_action["payload_json"])
        bound_run = (action_payload.get("binding") or {}).get("run")
        bound_run_id = bound_run.get("id") if isinstance(bound_run, dict) else None
        action_applies = bound_run_id in {None, accepted_run_id}
        if action_applies and latest_action["event_type"] == "CEO_WORK_ORDER_APPROVED":
            return None
        if (
            action_applies
            and latest_action["event_type"] == "CEO_WORK_ORDER_CHANGE_REQUESTED"
        ):
            return None
    if pending_decisions:
        return "CEO_DECISION_REQUIRED"
    if item.get("review_status") in {"CHANGES_REQUIRED", "REPAIR_REQUIRED"}:
        return "CEO_DECISION_REQUIRED"
    latest_outcome = item.get("run_outcome") or item.get("run_status")
    if accepted_run_id is None and latest_outcome in {
        "ERROR",
        "EXPIRED",
        "TESTS_FAILED",
        "USAGE_LIMIT_EXCEEDED",
        "COST_LIMIT_EXCEEDED",
        "WRITE_PERMISSION_DENIED",
    }:
        return "CEO_DECISION_REQUIRED"
    if accepted_run_id is not None or item.get("status") in {
        "VERIFIED",
        "COMPLETED",
        "AWAITING_REREVIEW",
    }:
        return "APPROVAL_PENDING"
    return None


def read_dashboard(
    db_path: str | Path,
    *,
    backup_dir: str | Path | None = None,
) -> dict[str, Any]:
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
            run_rows = connection.execute(
                """
                SELECT id, status, outcome, cost_usd, duration_seconds,
                       execution_id, fence_token,
                       payload_json, created_at
                FROM runs
                WHERE work_order_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (row["id"],),
            ).fetchall()
            item["runs"] = []
            for run_row in run_rows:
                run = dict(run_row)
                payload = json.loads(run.pop("payload_json"))
                run["usage_total_tokens"] = _run_tokens(payload)
                run["cost_status"] = payload.get("cost_status")
                run["usage_status"] = payload.get("usage_status")
                run["workspace_branch"] = payload.get("workspace_branch")
                run["production_request_id"] = payload.get(
                    "production_request_id"
                )
                run["diagnostic_only"] = payload.get("diagnostic_only")
                run["production_execution"] = payload.get(
                    "production_execution"
                )
                item["runs"].append(run)
            item["request_groups"] = _request_groups(item["runs"])
            item["cost_summary"] = (
                item["request_groups"][0]["summary"]
                if item["request_groups"]
                else _cost_summary(item["runs"])
            )
            rubric = connection.execute(
                """
                SELECT payload_json FROM evidence
                WHERE work_order_id = ? AND kind = 'RUBRIC_REPORT'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
            item["rubric"] = json.loads(rubric["payload_json"]) if rubric else None
            latest_action = connection.execute(
                """
                SELECT event_type, payload_json, occurred_at, sequence
                FROM events
                WHERE aggregate_type = 'WorkOrder' AND aggregate_id = ?
                  AND event_type IN (
                    'CEO_WORK_ORDER_APPROVED',
                    'CEO_WORK_ORDER_CHANGE_REQUESTED'
                  )
                ORDER BY sequence DESC LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
            pending_decisions = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM decisions
                    WHERE work_order_id = ?
                      AND status IN ('PENDING', 'ACTION_REQUIRED')
                    """,
                    (row["id"],),
                ).fetchone()[0]
            )
            item["attention_kind"] = _attention_kind(
                item,
                latest_action=latest_action,
                pending_decisions=pending_decisions,
            )
            item["last_ceo_action"] = (
                None
                if latest_action is None
                else {
                    "event_type": latest_action["event_type"],
                    "occurred_at": latest_action["occurred_at"],
                }
            )
            items.append(item)
        priority = {
            "APPROVAL_PENDING": 0,
            "CEO_DECISION_REQUIRED": 1,
            None: 2,
        }
        items.sort(key=lambda item: (priority[item["attention_kind"]], item["id"]))
        return {
            "read_only": True,
            "work_orders": items,
            "attention_count": sum(
                item["attention_kind"] is not None for item in items
            ),
            "approval_pending_count": sum(
                item["attention_kind"] == "APPROVAL_PENDING" for item in items
            ),
            "decision_required_count": sum(
                item["attention_kind"] == "CEO_DECISION_REQUIRED" for item in items
            ),
            "last_backup": _latest_backup(backup_dir),
        }
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


def record_dashboard_action(
    company: CompanyOS,
    work_order_id: str,
    *,
    action: str,
    request_text: str = "",
    idempotency_key: str,
) -> dict[str, str]:
    request_text = request_text.strip()
    if action not in {"approve", "request-change"}:
        raise ValidationError(f"unsupported dashboard action: {action}")
    if action == "request-change" and not request_text:
        raise ValidationError("revision request must not be empty")
    if len(request_text) > 2_000:
        raise ValidationError("revision request exceeds 2000 characters")
    if action == "approve" and request_text:
        raise ValidationError("approve action must not include a revision request")
    event_type = (
        "CEO_WORK_ORDER_APPROVED"
        if action == "approve"
        else "CEO_WORK_ORDER_CHANGE_REQUESTED"
    )
    command_payload = {
        "work_order_id": work_order_id,
        "action": action,
        "request": request_text,
    }

    def operation(connection: sqlite3.Connection) -> dict[str, str]:
        work = connection.execute(
            "SELECT venture_id FROM work_orders WHERE id = ?", (work_order_id,)
        ).fetchone()
        if work is None:
            raise NotFoundError(f"WorkOrder not found: {work_order_id}")
        binding = _work_binding(connection, work_order_id)
        payload: dict[str, Any] = {
            "actor": "CEO",
            "source": "LOCAL_DASHBOARD_CLI",
            "binding": binding,
        }
        if request_text:
            payload["request"] = request_text
        event = company.store.append_event(
            event_type,
            aggregate_type="WorkOrder",
            aggregate_id=work_order_id,
            venture_id=work["venture_id"],
            payload=payload,
            connection=connection,
        )
        return {
            "status": "RECORDED",
            "event_id": str(event["id"]),
            "event_type": event_type,
            "work_order_id": work_order_id,
        }

    try:
        result = company.store.run_idempotent(
            idempotency_key,
            "record_dashboard_action",
            command_payload,
            operation,
        )
    except (IdempotencyConflict, IdempotencyInProgress) as exc:
        raise ConflictError(str(exc)) from exc
    assert isinstance(result, dict)
    return {str(key): str(value) for key, value in result.items()}


def _format_tokens(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else "측정 불가"


def _format_seconds(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "측정 불가"
    return f"{float(value):,.1f}초"


def _format_usd(value: Any, *, unknown: bool = False) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "측정 불가"
    suffix = " + 미상" if unknown else ""
    return f"${float(value):,.4f}{suffix}"


def _render_page(snapshot: dict[str, Any], csrf_token: str, preview_url: str) -> str:
    cards: list[str] = []
    csrf = html.escape(csrf_token, quote=True)
    for item in snapshot["work_orders"]:
        rubric = item.get("rubric") or {}
        summary = item["cost_summary"]
        attention_kind = item.get("attention_kind")
        if attention_kind == "APPROVAL_PENDING":
            badge = '<span class="badge approval">승인 대기</span>'
        elif attention_kind == "CEO_DECISION_REQUIRED":
            badge = '<span class="badge decision">CEO 판단 필요</span>'
        else:
            badge = '<span class="badge normal">진행 중</span>'

        run_rows: list[str] = []
        for run in item.get("runs", []):
            outcome = html.escape(str(run.get("outcome") or run.get("status")))
            run_rows.append(
                "<tr>"
                f"<td><code>{html.escape(str(run['id']))}</code></td>"
                f"<td><span class=\"outcome\">{outcome}</span></td>"
                f"<td>{html.escape(_format_tokens(run.get('usage_total_tokens')))}</td>"
                f"<td>{html.escape(_format_seconds(run.get('duration_seconds')))}</td>"
                f"<td>{html.escape(_format_usd(run.get('cost_usd'), unknown=run.get('cost_usd') is None))}</td>"
                "</tr>"
            )
        history_html = (
            '<details><summary>실행 이력 전체 보기 '
            f'<span class="count">{len(run_rows)}건</span></summary>'
            '<div class="table-wrap"><table><thead><tr><th>Run</th><th>결과</th>'
            '<th>토큰</th><th>시간</th><th>USD</th></tr></thead><tbody>'
            + "".join(run_rows)
            + "</tbody></table></div></details>"
            if run_rows
            else '<p class="muted">실행 이력이 없습니다.</p>'
        )
        request_rows: list[str] = []
        for group in item.get("request_groups", []):
            request_summary = group["summary"]
            request_rows.append(
                "<tr>"
                f"<td><code>{html.escape(str(group['id']))}</code></td>"
                f"<td>{html.escape(_format_tokens(request_summary['accepted_tokens']))}</td>"
                f"<td>{html.escape(_format_tokens(request_summary['request_total_tokens']))}</td>"
                f"<td>{request_summary['attempt_count']}회</td>"
                "</tr>"
            )
        requests_html = (
            '<details open><summary>요청별 원가 '
            f'<span class="count">{len(request_rows)}건</span></summary>'
            '<div class="table-wrap"><table><thead><tr><th>요청</th>'
            '<th>채택 토큰</th><th>전체 토큰</th><th>시도</th></tr></thead><tbody>'
            + "".join(request_rows)
            + "</tbody></table></div></details>"
            if request_rows
            else ""
        )
        work_order_id = html.escape(str(item["id"]), quote=True)
        cards.append(
            '<article class="work-card">'
            '<div class="card-head"><div>'
            f"{badge}<h2>{html.escape(str(item['title']))}</h2>"
            f'<code class="work-id">{html.escape(str(item["id"]))}</code>'
            '</div><div class="state">'
            '<span>현재 상태</span>'
            f'<strong>{html.escape(str(item["status"]))}</strong>'
            "</div></div>"
            '<div class="cost-grid">'
            '<section><span class="eyebrow">최근 요청 채택 Run 원가</span>'
            f'<strong>{html.escape(_format_tokens(summary["accepted_tokens"]))} 토큰</strong>'
            f'<small>{html.escape(_format_seconds(summary["accepted_duration_seconds"]))} · '
            f'{html.escape(_format_usd(summary["accepted_cost_usd"], unknown=summary["accepted_cost_usd"] is None))}</small></section>'
            '<section><span class="eyebrow">최근 요청 전체 원가</span>'
            f'<strong>{html.escape(_format_tokens(summary["request_total_tokens"]))} 토큰</strong>'
            f'<small>{summary["attempt_count"]}회 시도 · '
            f'{html.escape(_format_seconds(summary["request_total_duration_seconds"]))} · '
            f'{html.escape(_format_usd(summary["request_total_cost_usd"], unknown=summary["request_has_unknown_usd"]))}</small></section>'
            "</div>"
            f"{requests_html}"
            '<div class="signals">'
            f'<span>평가 <strong>{html.escape(str(rubric.get("verdict", "없음")))}</strong> '
            f'{html.escape(str(rubric.get("score", "-")))}</span>'
            f'<span>독립 검수 <strong>{html.escape(str(item.get("review_status") or "미요청"))}</strong></span>'
            f'<a href="{html.escape(preview_url, quote=True)}" target="_blank" rel="noreferrer">미리보기 열기 ↗</a>'
            "</div>"
            f"{history_html}"
            '<div class="actions"><div><strong>CEO 작업</strong>'
            '<p>버튼은 원장 상태를 직접 덮어쓰지 않습니다. 승인 Event만 추가하며 '
            '모든 쓰기는 별도 company CLI 명령으로 기록됩니다.</p></div>'
            f'<form method="post" action="/approve"><input type="hidden" name="csrf" value="{csrf}">'
            f'<input type="hidden" name="work_order_id" value="{work_order_id}">'
            '<button class="primary" type="submit">승인 Event 추가</button></form>'
            f'<form class="change-form" method="post" action="/request-change"><input type="hidden" name="csrf" value="{csrf}">'
            f'<input type="hidden" name="work_order_id" value="{work_order_id}">'
            '<input name="request" maxlength="2000" required placeholder="수정할 내용을 구체적으로 입력">'
            '<button type="submit">수정 요청</button></form></div>'
            "</article>"
        )

    backup = snapshot.get("last_backup")
    backup_text = (
        "백업 없음"
        if backup is None
        else f"{html.escape(str(backup['created_at']))} · {html.escape(str(backup['name']))}"
    )
    return """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AI Company OS</title>
<style>
:root{color-scheme:light;--ink:#172033;--muted:#657087;--line:#dce2ec;--panel:#fff;
--bg:#eef2f7;--navy:#14213d;--blue:#2952cc;--amber:#b86600;--green:#16734a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,
"Pretendard",system-ui,-apple-system,"Segoe UI",sans-serif}header{background:linear-gradient(135deg,#101a32,#243a73);
color:white;padding:36px max(24px,calc((100vw - 1120px)/2)) 32px}header p{margin:7px 0 0;color:#cbd5ef}
.header-row{display:flex;align-items:flex-end;justify-content:space-between;gap:24px}.header-row h1{margin:0;font-size:30px}
.backup{font-size:13px;background:#ffffff13;border:1px solid #ffffff24;border-radius:12px;padding:12px 14px}
.backup strong{display:block;color:white;margin-bottom:3px}.shell{max-width:1120px;margin:0 auto;padding:24px}
.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:-42px;margin-bottom:22px}
.metric{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:0 8px 25px #14213d12}
.metric span,.eyebrow{display:block;color:var(--muted);font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase}
.metric strong{display:block;font-size:27px;margin-top:5px}.section-title{display:flex;justify-content:space-between;align-items:center}
.section-title h2{font-size:17px}.read-only{color:var(--green);font-size:13px}.work-card{background:var(--panel);border:1px solid var(--line);
border-radius:16px;padding:22px;margin:14px 0;box-shadow:0 4px 18px #14213d0a}.card-head{display:flex;justify-content:space-between;gap:20px}
.card-head h2{margin:9px 0 4px;font-size:19px}.work-id{font-size:11px;color:var(--muted)}.state{text-align:right}.state span{display:block;
font-size:11px;color:var(--muted);margin-bottom:4px}.badge{font-size:11px;font-weight:800;padding:5px 9px;border-radius:999px}.approval{background:#fff1dc;color:#8a4b00}
.decision{background:#ffe9e7;color:#9d2c21}.normal{background:#e6f4ed;color:#12623f}.cost-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:20px 0}
.cost-grid section{background:#f7f9fc;border:1px solid #e6ebf2;border-radius:12px;padding:15px}.cost-grid strong{display:block;font-size:20px;margin:5px 0}
.cost-grid small{color:var(--muted)}.signals{display:flex;gap:18px;align-items:center;flex-wrap:wrap;border-top:1px solid var(--line);border-bottom:1px solid var(--line);
padding:13px 0;margin-bottom:14px;font-size:13px}.signals a{margin-left:auto;color:var(--blue);font-weight:700;text-decoration:none}details{font-size:13px}
summary{cursor:pointer;font-weight:700}.count{color:var(--muted);font-weight:500}.table-wrap{overflow:auto;margin-top:12px}table{border-collapse:collapse;width:100%;font-size:12px}
th,td{text-align:left;padding:9px;border-bottom:1px solid #edf0f5;white-space:nowrap}th{color:var(--muted)}.outcome{font-weight:700}.actions{margin-top:18px;
display:grid;grid-template-columns:1.2fr auto 1.6fr;gap:12px;align-items:end;background:#f8fafc;border-radius:12px;padding:14px}.actions p{font-size:12px;color:var(--muted);margin:4px 0 0}
form{display:flex;gap:8px}input{width:100%;min-width:180px;padding:10px 12px;border:1px solid #cbd3df;border-radius:9px;background:white}button{border:1px solid #b9c3d3;
background:white;border-radius:9px;padding:10px 13px;font-weight:750;cursor:pointer;white-space:nowrap}.primary{background:var(--blue);border-color:var(--blue);color:white}.muted{color:var(--muted)}
@media(max-width:800px){.header-row,.card-head{align-items:flex-start;flex-direction:column}.summary,.cost-grid{grid-template-columns:1fr}.summary{margin-top:-24px}.actions{grid-template-columns:1fr}.state{text-align:left}.signals a{margin-left:0}.change-form{flex-direction:column}}
</style></head><body><header><div class="header-row"><div><h1>AI Company OS</h1>
<p>오늘의 승인, 판단, 실행 원가를 한 화면에서 확인합니다.</p></div><div class="backup"><strong>마지막 백업</strong>""" + backup_text + """</div></div></header>
<main class="shell"><section class="summary"><div class="metric"><span>CEO 확인 필요</span><strong>""" + str(snapshot["attention_count"]) + """</strong></div>
<div class="metric"><span>승인 대기</span><strong>""" + str(snapshot["approval_pending_count"]) + """</strong></div>
<div class="metric"><span>판단 필요</span><strong>""" + str(snapshot["decision_required_count"]) + """</strong></div></section>
<div class="section-title"><h2>WorkOrder 운영 현황</h2><span class="read-only">● SQLite read-only 조회</span></div>""" + "".join(cards) + "</main></body></html>"


class DashboardServer(ThreadingHTTPServer):
    csrf_token: str


DashboardActionRunner = Callable[[str, str, str, str], dict[str, Any]]


def _cli_action_runner(
    root: Path,
    database: Path,
) -> DashboardActionRunner:
    def run_action(
        action: str,
        work_order_id: str,
        request_text: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        command = [
            sys.executable,
            "-m",
            "company_os.cli",
            "--root",
            str(root),
            "--db",
            str(database),
            "dashboard-action",
            action,
            work_order_id,
            "--idempotency-key",
            idempotency_key,
        ]
        if action == "request-change":
            command.extend(["--request", request_text])
        environment = os.environ.copy()
        source_root = str((root / "src").resolve())
        existing_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (source_root, existing_pythonpath) if part
        )
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                check=False,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError(f"dashboard CLI action could not run: {exc}") from exc
        output = completed.stdout.strip() or completed.stderr.strip()
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                "dashboard CLI action returned invalid JSON"
            ) from exc
        if completed.returncode != 0:
            message = payload.get("message") if isinstance(payload, dict) else output
            raise ValidationError(f"dashboard CLI action failed: {message}")
        if not isinstance(payload, dict):
            raise ValidationError("dashboard CLI action returned a non-object result")
        return payload

    return run_action


def create_dashboard_server(
    root: str | Path,
    db_path: str | Path,
    *,
    port: int = 8780,
    preview_url: str = "http://127.0.0.1:8765/",
    backup_dir: str | Path | None = None,
    action_runner: DashboardActionRunner | None = None,
) -> DashboardServer:
    root_path = Path(root).resolve()
    database = Path(db_path).resolve()
    backups = (
        Path(backup_dir).resolve()
        if backup_dir is not None
        else database.parent / "backups"
    )
    cli_action = action_runner or _cli_action_runner(root_path, database)
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
            page = _render_page(
                read_dashboard(database, backup_dir=backups),
                csrf_token,
                preview_url,
            )
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
                request_text = values.get("request", [""])[0]
                if self.path == "/approve":
                    action = "approve"
                elif self.path == "/request-change":
                    action = "request-change"
                else:
                    self._send(404, b"not found", "text/plain")
                    return
                key_material = "\0".join(
                    (csrf_token, action, work_order_id, request_text)
                ).encode("utf-8")
                idempotency_key = (
                    "dashboard-action:" + sha256(key_material).hexdigest()
                )
                result = cli_action(
                    action,
                    work_order_id,
                    request_text,
                    idempotency_key,
                )
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except (ConflictError, NotFoundError, ValidationError, ValueError) as exc:
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
    backup_dir: str | Path | None = None,
) -> None:
    server = create_dashboard_server(
        root,
        db_path,
        port=port,
        preview_url=preview_url,
        backup_dir=backup_dir,
    )
    print(f"dashboard=http://127.0.0.1:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
