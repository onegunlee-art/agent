"""On-demand, outbound-only CEO attention notifications."""

from __future__ import annotations

import hashlib
import json
import os
import smtplib
import re
import urllib.request
from urllib.parse import urlsplit
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

from .application import CompanyOS
from .dashboard import read_dashboard
from .errors import ConflictError, ValidationError
from .storage import IdempotencyConflict
from .utils import atomic_write_json, canonical_json, utc_now


NotificationSender = Callable[[dict[str, Any], dict[str, Any]], None]
_SUMMARY = {
    "APPROVAL_PENDING": "완료 결과의 CEO 승인이 필요합니다.",
    "CEO_DECISION_REQUIRED": "진행을 위한 CEO 판단이 필요합니다.",
}


def _dashboard_link(value: Any) -> str:
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        if (parsed is None or parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.username is not None or parsed.password is not None
                or parsed.port is None or not 1 <= parsed.port <= 65535
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(char.isspace() for char in value)):
            raise ValueError("invalid local dashboard link")
    except ValueError as exc:
        raise ValidationError("dashboard link must be a bare http://127.0.0.1:PORT/ URL") from exc
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The CEO configured one endpoint, not an arbitrary redirect chain.
        return None


def _load_settings(
    config_path: str | Path | None,
    *,
    dashboard_url: str,
) -> dict[str, Any]:
    if config_path is None:
        return {
            "channel": "local_file",
            "dashboard_url": _dashboard_link(dashboard_url),
        }
    source = Path(config_path).absolute()
    if source.is_symlink() or not source.is_file():
        raise ValidationError("notification config must be a regular file")
    try:
        settings = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("notification config must be valid UTF-8 JSON") from exc
    if not isinstance(settings, dict) or settings.get("schema_version") != 1:
        raise ValidationError("unsupported notification config schema")
    channel = settings.get("channel")
    if channel not in {"webhook", "email"}:
        raise ValidationError("notification channel must be webhook or email")
    target = settings.get("target")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("notification config requires a single target string")
    target = target.strip()
    if channel == "webhook":
        try:
            parsed = urlsplit(target)
            if (parsed.scheme != "https" or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.fragment or any(char.isspace() for char in target)
                    or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
                raise ValueError("invalid webhook")
        except ValueError as exc:
            raise ValidationError("notification webhook requires one HTTPS endpoint without userinfo") from exc
    if channel == "email" and re.fullmatch(r"[^\s@,;<>:]+@[^\s@,;<>:]+", target) is None:
        raise ValidationError("notification email requires one plain mailbox address")
    configured_dashboard = _dashboard_link(settings.get("dashboard_url", dashboard_url))
    return {**settings, "target": target, "dashboard_url": configured_dashboard}


def _default_sender(settings: dict[str, Any], message: dict[str, Any]) -> None:
    content = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if settings["channel"] == "webhook":
        request = urllib.request.Request(
            str(settings["target"]),
            data=content,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise OSError(f"notification webhook returned HTTP {response.status}")
        return

    host = settings.get("smtp_host")
    if not isinstance(host, str) or not host:
        raise ValidationError("email notification requires smtp_host")
    port = int(settings.get("smtp_port", 587))
    from_address = str(settings.get("from_address", settings["target"]))
    email = EmailMessage()
    email["Subject"] = "AI Company OS CEO 확인 필요"
    email["From"] = from_address
    email["To"] = str(settings["target"])
    email.set_content(
        f"{message['summary']}\nWorkOrder: {message['work_order_id']}\n"
        f"현황판: {message['dashboard_url']}\n"
    )
    with smtplib.SMTP(host, port, timeout=10) as client:
        if bool(settings.get("starttls", True)):
            client.starttls()
        username = os.environ.get("AI_COMPANY_OS_EMAIL_USERNAME")
        password = os.environ.get("AI_COMPANY_OS_EMAIL_PASSWORD")
        if username and password:
            client.login(username, password)
        client.send_message(email)


def _latest_source_event(
    events: list[dict[str, Any]], work_order_id: str
) -> dict[str, Any] | None:
    matched = []
    for event in events:
        if str(event.get("event_type", "")).startswith("NOTIFICATION_"):
            continue
        payload = event.get("payload")
        if event.get("aggregate_id") == work_order_id or (
            isinstance(payload, dict) and payload.get("work_order_id") == work_order_id
        ):
            matched.append(event)
    return matched[-1] if matched else None


def _notification_message(
    *,
    work_order_id: str,
    attention_kind: str,
    source_event_id: str,
    dashboard_url: str,
) -> dict[str, Any]:
    core = {
        "schema_version": 1,
        "work_order_id": work_order_id,
        "attention_kind": attention_kind,
        "summary": _SUMMARY[attention_kind],
        "dashboard_url": dashboard_url,
        "source_event_id": source_event_id,
    }
    notification_id = "notification_" + hashlib.sha256(
        canonical_json(core).encode("utf-8")
    ).hexdigest()[:32]
    return {"notification_id": notification_id, **core, "created_at": utc_now()}


def dispatch_pending_notifications(
    company: CompanyOS,
    *,
    outbox: str | Path,
    config_path: str | Path | None = None,
    dashboard_url: str = "http://127.0.0.1:8780/",
    sender: NotificationSender | None = None,
) -> dict[str, Any]:
    """Send current attention items once; transport failures never raise."""

    settings = _load_settings(config_path, dashboard_url=dashboard_url)
    delivery = sender or _default_sender
    destination = Path(outbox).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    snapshot = read_dashboard(company.db_path)
    events = company.events()
    sent: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    skipped = 0

    for item in snapshot["work_orders"]:
        attention_kind = item.get("attention_kind")
        if attention_kind not in _SUMMARY:
            continue
        work_order_id = str(item["id"])
        source_event = _latest_source_event(events, work_order_id)
        if source_event is None:
            skipped += 1
            continue
        message = _notification_message(
            work_order_id=work_order_id,
            attention_kind=str(attention_kind),
            source_event_id=str(source_event["id"]),
            dashboard_url=str(settings["dashboard_url"]),
        )
        # Local fallback is not external delivery. Bind deduplication to the
        # selected route without placing webhook secrets/mailboxes in Events.
        route_sha256 = hashlib.sha256(canonical_json({
            key: settings.get(key) for key in ("channel", "target", "smtp_host", "smtp_port")
        }).encode("utf-8")).hexdigest()
        key = f"dispatch-notification:{message['notification_id']}:{route_sha256}"
        claim_payload = {
            "notification_id": message["notification_id"],
            "work_order_id": work_order_id,
            "attention_kind": attention_kind,
            "source_event_id": message["source_event_id"],
            "channel": settings["channel"],
            "route_sha256": route_sha256,
        }
        try:
            with company.store.transaction() as connection:
                claim = company.store.claim_idempotency(
                    key,
                    "dispatch_notification",
                    claim_payload,
                    connection=connection,
                )
                if not claim.is_new:
                    skipped += 1
                    continue
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc

        output_path = destination / f"{message['notification_id']}.json"
        try:
            if not output_path.exists():
                atomic_write_json(output_path, message)
            if settings["channel"] != "local_file":
                delivery(settings, message)
        except Exception as exc:  # transport failure is audit data, not production failure
            with company.store.transaction() as connection:
                event = company.store.append_event(
                    "NOTIFICATION_FAILED",
                    aggregate_type="WorkOrder",
                    aggregate_id=work_order_id,
                    payload={
                        **claim_payload,
                        "error_type": type(exc).__name__,
                        "retry_allowed": True,
                    },
                    connection=connection,
                )
                connection.execute(
                    "DELETE FROM idempotency WHERE key = ? AND command = ? "
                    "AND status = 'CLAIMED'",
                    (key, "dispatch_notification"),
                )
            failures.append(
                {
                    "work_order_id": work_order_id,
                    "event_id": str(event["id"]),
                    "error_type": type(exc).__name__,
                }
            )
            continue

        with company.store.transaction() as connection:
            event = company.store.append_event(
                "NOTIFICATION_SENT",
                aggregate_type="WorkOrder",
                aggregate_id=work_order_id,
                payload={
                    **claim_payload,
                    "outbox_path": output_path.name,
                    "contains_customer_data": False,
                },
                connection=connection,
            )
            result = {
                "status": "SENT",
                "notification_id": message["notification_id"],
                "work_order_id": work_order_id,
                "event_id": str(event["id"]),
                "channel": settings["channel"],
            }
            company.store.complete_idempotency(
                key,
                result,
                command="dispatch_notification",
                connection=connection,
            )
        sent.append(result)

    return {
        "status": "DISPATCH_COMPLETE",
        "sent_count": len(sent),
        "failed_count": len(failures),
        "skipped_count": skipped,
        "sent": sent,
        "failures": failures,
    }
