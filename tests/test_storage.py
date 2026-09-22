from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from company_os.storage import IdempotencyConflict, StateStore


TIMESTAMP = "2026-01-01T00:00:00.000000Z"


def _idea(record_id: str, text: str = "A synthetic deterministic idea") -> dict[str, str]:
    return {
        "id": record_id,
        "text": text,
        "created_at": TIMESTAMP,
        "updated_at": TIMESTAMP,
    }


def test_foreign_keys_are_enabled_and_enforced(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"

    with StateStore(database) as store:
        assert store.foreign_keys_enabled() is True

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            store.append_event(
                "VENTURE_MISSING",
                venture_id="venture-does-not-exist",
                event_id="event-invalid-foreign-key",
            )

        assert store.get_row("events", "event-invalid-foreign-key") is None


def test_file_database_uses_wal_and_configured_busy_timeout(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    configured_timeout_ms = 1_739

    with StateStore(database, busy_timeout_ms=configured_timeout_ms) as store:
        assert store.journal_mode().lower() == "wal"
        assert store.scalar("PRAGMA busy_timeout") == configured_timeout_ms


def test_transaction_rolls_back_atomically_and_reopens_cleanly(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = StateStore(database).initialize()
    store.insert_row("ideas", _idea("idea-committed"))

    with pytest.raises(RuntimeError, match="synthetic transaction failure"):
        with store.transaction() as connection:
            store.insert_row(
                "ideas",
                _idea("idea-rolled-back"),
                connection=connection,
            )
            store.append_event(
                "PARTIAL_TRANSITION",
                aggregate_type="idea",
                aggregate_id="idea-rolled-back",
                event_id="event-rolled-back",
                connection=connection,
            )
            raise RuntimeError("synthetic transaction failure")

    assert store.get_row("ideas", "idea-rolled-back") is None
    assert store.get_row("events", "event-rolled-back") is None
    store.close()

    with StateStore(database) as reopened:
        assert reopened.get_row("ideas", "idea-committed") is not None
        assert reopened.get_row("ideas", "idea-rolled-back") is None
        assert reopened.get_row("events", "event-rolled-back") is None
        assert reopened.integrity_check() == "ok"


def test_idempotent_command_replays_result_and_rejects_changed_payload(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    calls: list[str] = []
    request = {"priority": 1, "text": "synthetic request"}

    with StateStore(database) as store:

        def perform(connection: sqlite3.Connection) -> dict[str, object]:
            calls.append("called")
            store.append_event(
                "COMMAND_EXECUTED",
                payload={"source": "idempotency-test"},
                event_id="event-idempotent-result",
                connection=connection,
            )
            return {"accepted": True, "event_id": "event-idempotent-result"}

        first_result = store.run_idempotent(
            "request-001",
            "create-synthetic-record",
            request,
            perform,
        )

    with StateStore(database) as reopened:

        def must_not_run(connection: sqlite3.Connection) -> dict[str, object]:
            raise AssertionError("an exact idempotency replay executed twice")

        replayed_result = reopened.run_idempotent(
            "request-001",
            "create-synthetic-record",
            {"text": "synthetic request", "priority": 1},
            must_not_run,
        )

        assert replayed_result == first_result
        assert calls == ["called"]
        assert reopened.scalar("SELECT count(*) FROM events") == 1
        assert reopened.get_row("idempotency", "request-001")["status"] == "COMPLETED"

        with pytest.raises(IdempotencyConflict, match="different command or payload"):
            reopened.run_idempotent(
                "request-001",
                "create-synthetic-record",
                {"priority": 2, "text": "synthetic request"},
                must_not_run,
            )

        assert reopened.scalar("SELECT count(*) FROM events") == 1


def test_concurrent_writes_succeed_from_separate_store_connections(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    with StateStore(database):
        pass

    writer_count = 4
    events_per_writer = 8
    ready = threading.Barrier(writer_count)

    def write_events(writer: int) -> list[str]:
        event_ids: list[str] = []
        with StateStore(database, busy_timeout_ms=15_000) as store:
            ready.wait(timeout=10)
            for index in range(events_per_writer):
                event_id = f"event-writer-{writer}-{index}"
                store.append_event(
                    "CONCURRENT_WRITE",
                    payload={"index": index, "writer": writer},
                    event_id=event_id,
                    occurred_at=TIMESTAMP,
                )
                event_ids.append(event_id)
        return event_ids

    with ThreadPoolExecutor(max_workers=writer_count) as executor:
        futures = [executor.submit(write_events, writer) for writer in range(writer_count)]
        written_ids = {
            event_id
            for future in futures
            for event_id in future.result(timeout=30)
        }

    with StateStore(database) as reopened:
        rows = reopened.list_rows("events")

    assert len(rows) == writer_count * events_per_writer
    assert {row["id"] for row in rows} == written_ids
    assert len(written_ids) == writer_count * events_per_writer


def test_event_ledger_database_triggers_reject_update_and_delete(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"

    with StateStore(database) as store:
        store.append_event(
            "ORIGINAL_EVENT",
            event_id="event-immutable",
            occurred_at=TIMESTAMP,
        )

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE events SET event_type = ? WHERE id = ?",
                    ("ALTERED_EVENT", "event-immutable"),
                )

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with store.transaction() as connection:
                connection.execute(
                    "DELETE FROM events WHERE id = ?",
                    ("event-immutable",),
                )

        row = store.get_row("events", "event-immutable")
        assert row is not None
        assert row["event_type"] == "ORIGINAL_EVENT"


def test_reopen_recovers_after_process_dies_with_an_uncommitted_write(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    with StateStore(database) as store:
        store.append_event(
            "COMMITTED_BEFORE_INTERRUPTION",
            event_id="event-committed",
            occurred_at=TIMESTAMP,
        )

    source_root = Path(__file__).resolve().parents[1] / "src"
    child_program = """
import os
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[2])
from company_os.storage import StateStore

store = StateStore(Path(sys.argv[1])).initialize()
connection = store.connection
connection.execute("BEGIN IMMEDIATE")
store.append_event(
    "INTERRUPTED_WRITE",
    event_id="event-uncommitted",
    occurred_at="2026-01-01T00:00:00.000000Z",
    connection=connection,
)
os._exit(23)
"""
    interrupted = subprocess.run(
        [sys.executable, "-c", child_program, str(database), str(source_root)],
        check=False,
        timeout=20,
    )
    assert interrupted.returncode == 23

    with StateStore(database) as recovered:
        assert recovered.integrity_check() == "ok"
        assert [row["id"] for row in recovered.list_rows("events")] == [
            "event-committed"
        ]


def test_jsonl_export_preserves_sequence_order_and_explicit_event_ids(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    destination = tmp_path / "audit" / "events.jsonl"
    specifications = [
        ("event-z", "FIRST", {"position": 1}),
        ("event-a", "SECOND", {"position": 2}),
        ("event-m", "THIRD", {"position": 3}),
    ]

    with StateStore(database) as store:
        for event_id, event_type, payload in specifications:
            store.append_event(
                event_type,
                payload=payload,
                event_id=event_id,
                occurred_at=TIMESTAMP,
            )
        exported = store.export_events_jsonl(destination)

    assert exported == destination
    records = [
        json.loads(line)
        for line in destination.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["sequence"] for record in records] == [1, 2, 3]
    assert [record["id"] for record in records] == [
        "event-z",
        "event-a",
        "event-m",
    ]
    assert [record["event_type"] for record in records] == [
        "FIRST",
        "SECOND",
        "THIRD",
    ]
    assert [record["payload"] for record in records] == [
        {"position": 1},
        {"position": 2},
        {"position": 3},
    ]
    assert all("payload_json" not in record for record in records)
