from __future__ import annotations

import json
from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.cli import build_parser
from company_os.errors import ValidationError
from company_os.model_executor import ExecutorOutcome
from company_os.notifications import dispatch_pending_notifications, _load_settings

from .helpers import build_venture


def _successful_outcome() -> ExecutorOutcome:
    return ExecutorOutcome(
        ok=True,
        label="DONE",
        cost_usd=None,
        cost_unknown=True,
        duration_seconds=1.0,
        usage={"input_tokens": 90, "output_tokens": 10},
        usage_status="WITHIN_LIMIT",
        usage_total_tokens=100,
    )


def _failed_outcome() -> ExecutorOutcome:
    return ExecutorOutcome(
        ok=False,
        label="TESTS_FAILED",
        cost_usd=None,
        cost_unknown=True,
        duration_seconds=1.0,
        usage={"input_tokens": 40, "output_tokens": 10},
        usage_status="WITHIN_LIMIT",
        usage_total_tokens=50,
    )


def test_local_notification_contains_only_safe_attention_fields(tmp_path: Path) -> None:
    root = tmp_path / "company"
    outbox = tmp_path / "outbox"
    secret = "CUSTOMER-SECRET-MARKER-99"
    with CompanyOS(root) as company:
        _, _, _, work_order = build_venture(company, f"notify-{secret}")
        company.record_model_execution(
            work_order.id,
            _successful_outcome(),
            idempotency_key="notify-success-run",
        )

        result = dispatch_pending_notifications(
            company,
            outbox=outbox,
            dashboard_url="http://127.0.0.1:8780/",
        )
        replay = dispatch_pending_notifications(
            company,
            outbox=outbox,
            dashboard_url="http://127.0.0.1:8780/",
        )
        events = company.events()

    assert result["sent_count"] == 1
    assert result["failed_count"] == 0
    assert replay["sent_count"] == 0
    assert replay["skipped_count"] == 1
    files = list(outbox.glob("*.json"))
    assert len(files) == 1
    notification = json.loads(files[0].read_text(encoding="utf-8"))
    rendered = json.dumps(notification, ensure_ascii=False)
    assert notification["work_order_id"] == work_order.id
    assert notification["attention_kind"] == "APPROVAL_PENDING"
    assert notification["dashboard_url"] == "http://127.0.0.1:8780/"
    assert secret not in rendered
    sent = [event for event in events if event["event_type"] == "NOTIFICATION_SENT"]
    assert len(sent) == 1
    assert sent[0]["payload"]["channel"] == "local_file"
    assert secret not in json.dumps(sent[0], ensure_ascii=False)


def test_config_allows_exactly_one_outbound_target_and_sender_is_injected(
    tmp_path: Path,
) -> None:
    config = tmp_path / "notification.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "channel": "webhook",
                "target": "https://notify.invalid/ceo",
                "dashboard_url": "http://127.0.0.1:8780/",
            }
        ),
        encoding="utf-8",
    )
    delivered: list[tuple[dict[str, object], dict[str, object]]] = []
    with CompanyOS(tmp_path / "company") as company:
        _, _, _, work_order = build_venture(company, "notify-webhook")
        company.record_model_execution(
            work_order.id,
            _successful_outcome(),
            idempotency_key="notify-webhook-run",
        )

        result = dispatch_pending_notifications(
            company,
            outbox=tmp_path / "outbox",
            config_path=config,
            sender=lambda settings, message: delivered.append((settings, message)),
        )

    assert result["sent_count"] == 1
    assert delivered[0][0]["channel"] == "webhook"
    assert delivered[0][1]["work_order_id"] == work_order.id

    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "channel": "webhook",
                "target": ["https://one.invalid", "https://two.invalid"],
            }
        ),
        encoding="utf-8",
    )
    with CompanyOS(tmp_path / "other") as company:
        with pytest.raises(ValueError, match="single target"):
            dispatch_pending_notifications(
                company,
                outbox=tmp_path / "other-outbox",
                config_path=config,
            )


def test_failed_run_generates_ceo_decision_required_notice(tmp_path: Path) -> None:
    with CompanyOS(tmp_path / "company") as company:
        _, _, _, work_order = build_venture(company, "notify-decision")
        company.record_model_execution(
            work_order.id,
            _failed_outcome(),
            idempotency_key="notify-decision-run",
        )

        result = dispatch_pending_notifications(
            company,
            outbox=tmp_path / "outbox",
        )

    assert result["sent_count"] == 1
    message = json.loads(
        next((tmp_path / "outbox").glob("*.json")).read_text(encoding="utf-8")
    )
    assert message["attention_kind"] == "CEO_DECISION_REQUIRED"
    assert message["summary"] == "진행을 위한 CEO 판단이 필요합니다."


def test_delivery_failure_is_recorded_and_does_not_raise(tmp_path: Path) -> None:
    config = tmp_path / "notification.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "channel": "email",
                "target": "ceo@example.invalid",
                "smtp_host": "smtp.example.invalid",
            }
        ),
        encoding="utf-8",
    )
    with CompanyOS(tmp_path / "company") as company:
        _, _, _, work_order = build_venture(company, "notify-failure")
        company.record_model_execution(
            work_order.id,
            _successful_outcome(),
            idempotency_key="notify-failure-run",
        )

        result = dispatch_pending_notifications(
            company,
            outbox=tmp_path / "outbox",
            config_path=config,
            sender=lambda _settings, _message: (_ for _ in ()).throw(
                OSError("synthetic transport failure")
            ),
        )
        events = company.events()

    assert result["sent_count"] == 0
    assert result["failed_count"] == 1
    assert result["failures"][0]["work_order_id"] == work_order.id
    assert any(event["event_type"] == "NOTIFICATION_FAILED" for event in events)


def test_notification_cli_has_dispatch_only_and_no_remote_approval_route() -> None:
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "notification",
            "dispatch",
            "--outbox",
            "local-outbox",
        ]
    )
    assert parsed.notification_command == "dispatch"
    with pytest.raises(SystemExit):
        parser.parse_args(["notification", "approve"])


@pytest.mark.parametrize("url", [
    "https://outside.invalid/", "http://127.0.0.1:8780@outside.invalid/",
    "http://127.0.0.1:8780/?customer=private", "http://127.0.0.1:8780/private",
    "http://127.0.0.1:99999/", "http://127.0.0.1:8780/#private",
])
def test_dashboard_link_is_local_and_cannot_carry_customer_content(url):
    with pytest.raises((ValueError, ValidationError)):
        _load_settings(None, dashboard_url=url)


@pytest.mark.parametrize("channel,target", [
    ("email", "a@example.invalid,b@example.invalid"),
    ("email", "a@example.invalid\nBcc: b@example.invalid"),
    ("webhook", "https://"),
    ("webhook", "https://user:password@notify.invalid/path"),
])
def test_destination_validation_rejects_ambiguous_or_multiple_targets(tmp_path, channel, target):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema_version": 1, "channel": channel, "target": target}), encoding="utf-8")
    with pytest.raises((ValueError, ValidationError)):
        _load_settings(config, dashboard_url="http://127.0.0.1:8780/")


def test_local_fallback_does_not_consume_later_external_delivery(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema_version": 1, "channel": "webhook", "target": "https://notify.invalid/ceo"}), encoding="utf-8")
    deliveries = []
    with CompanyOS(tmp_path / "company") as company:
        _, _, _, order = build_venture(company, "notify-fallback")
        company.record_model_execution(order.id, _successful_outcome(), idempotency_key="fallback-run")
        before = dict(company.store.query_one("SELECT * FROM work_orders WHERE id = ?", (order.id,)))
        def deliver(settings, message):
            assert not company.store.connection.in_transaction
            deliveries.append(message)
        assert dispatch_pending_notifications(company, outbox=tmp_path / "outbox")["sent_count"] == 1
        assert dispatch_pending_notifications(company, outbox=tmp_path / "outbox", config_path=config, sender=deliver)["sent_count"] == 1
        assert dispatch_pending_notifications(company, outbox=tmp_path / "outbox", config_path=config, sender=deliver)["skipped_count"] == 1
        assert dict(company.store.query_one("SELECT * FROM work_orders WHERE id = ?", (order.id,))) == before
    assert len(deliveries) == 1


def test_webhook_transport_disallows_redirects(monkeypatch):
    from company_os.notifications import _default_sender
    import urllib.request
    handlers = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Opener:
        def open(self, request, *, timeout):
            assert timeout == 10
            return Response()
    def build(*items):
        handlers.extend(items)
        return Opener()
    monkeypatch.setattr(urllib.request, "build_opener", build)
    def no_network(*args, **kwargs):
        raise AssertionError("transport must use the redirect-blocking opener")
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    _default_sender({"channel": "webhook", "target": "https://notify.invalid/one"}, {"summary": "safe"})
    redirect = next(item for item in handlers if isinstance(item, urllib.request.HTTPRedirectHandler))
    request = urllib.request.Request("https://notify.invalid/one", data=b"safe")
    assert redirect.redirect_request(request, None, 302, "Found", {}, "https://other.invalid/") is None
