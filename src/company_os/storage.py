"""Durable SQLite storage for the local Company OS.

SQLite is the canonical state store.  Filesystem artifacts are referenced by
path, while the append-only event ledger can be exported to JSONL for audit
and handoff purposes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, TypeVar
from uuid import uuid4


T = TypeVar("T")
JsonValue = Any


class StorageError(RuntimeError):
    """Base class for state-store errors."""


class IdempotencyConflict(StorageError):
    """An idempotency key was reused for a different command or payload."""


class IdempotencyInProgress(StorageError):
    """The same command is already claimed but has no recorded result."""


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    """Result of claiming an idempotency key."""

    key: str
    command: str
    payload_hash: str
    is_new: bool
    result: JsonValue | None
    status: str

    @property
    def reused(self) -> bool:
        return not self.is_new

    @property
    def completed(self) -> bool:
        return self.status == "COMPLETED"


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MISSING = object()


_TABLES = frozenset(
    {
        "ideas",
        "council_responses",
        "contracts",
        "ventures",
        "assumptions",
        "metrics",
        "experiments",
        "work_orders",
        "runs",
        "reviews",
        "review_required_changes",
        "review_change_resolutions",
        "evidence",
        "decisions",
        "approvals",
        "artifacts",
        "events",
        "global_state",
        "idempotency",
    }
)


_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS ideas (
    id              TEXT PRIMARY KEY,
    text            TEXT NOT NULL CHECK (length(trim(text)) > 0),
    status          TEXT NOT NULL DEFAULT 'NEW',
    metadata_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS council_responses (
    id              TEXT PRIMARY KEY,
    idea_id         TEXT NOT NULL REFERENCES ideas(id),
    role            TEXT NOT NULL CHECK (role IN ('cto', 'cpo', 'cmo')),
    version         INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    status          TEXT NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE', 'SUPERSEDED')),
    response_hash   TEXT NOT NULL,
    payload_json    TEXT NOT NULL CHECK (json_valid(payload_json)),
    source_path     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (idea_id, role, version),
    UNIQUE (idea_id, role, response_hash)
);

CREATE TABLE IF NOT EXISTS contracts (
    id              TEXT PRIMARY KEY,
    idea_id         TEXT NOT NULL REFERENCES ideas(id),
    decision_level  TEXT NOT NULL,
    min_decision_level TEXT NOT NULL DEFAULT 'FP_LITE',
    gate_status     TEXT NOT NULL DEFAULT 'PENDING',
    payload_json    TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ventures (
    id                      TEXT PRIMARY KEY,
    idea_id                 TEXT NOT NULL REFERENCES ideas(id),
    contract_id             TEXT NOT NULL UNIQUE REFERENCES contracts(id),
    status                  TEXT NOT NULL DEFAULT 'ACTIVE',
    workspace_path          TEXT NOT NULL,
    context_manifest_path   TEXT NOT NULL,
    metadata_json           TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assumptions (
    id              TEXT PRIMARY KEY,
    venture_id      TEXT NOT NULL REFERENCES ventures(id),
    contract_id     TEXT REFERENCES contracts(id),
    external_ref    TEXT,
    statement       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'UNTESTED',
    classification  TEXT NOT NULL DEFAULT 'ASSUMPTION',
    payload_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    id              TEXT PRIMARY KEY,
    venture_id      TEXT NOT NULL REFERENCES ventures(id),
    name            TEXT NOT NULL,
    formula         TEXT NOT NULL,
    unit            TEXT NOT NULL,
    time_window     TEXT NOT NULL,
    data_source     TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    payload_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (venture_id, name, version)
);

CREATE TABLE IF NOT EXISTS experiments (
    id              TEXT PRIMARY KEY,
    venture_id      TEXT NOT NULL REFERENCES ventures(id),
    assumption_id   TEXT REFERENCES assumptions(id),
    metric_id       TEXT REFERENCES metrics(id),
    status          TEXT NOT NULL DEFAULT 'PLANNED',
    payload_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_orders (
    id                  TEXT PRIMARY KEY,
    venture_id          TEXT NOT NULL REFERENCES ventures(id),
    experiment_id       TEXT REFERENCES experiments(id),
    title               TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'READY',
    specification_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(specification_json)),
    verifier_path       TEXT,
    verifier_sha256     TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id                  TEXT PRIMARY KEY,
    work_order_id       TEXT NOT NULL REFERENCES work_orders(id),
    status              TEXT NOT NULL,
    executor            TEXT NOT NULL,
    verifier_sha256     TEXT,
    payload_json        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id                      TEXT PRIMARY KEY,
    work_order_id           TEXT NOT NULL REFERENCES work_orders(id),
    run_id                  TEXT REFERENCES runs(id),
    schema_version          INTEGER NOT NULL DEFAULT 1,
    parent_review_id        TEXT REFERENCES reviews(id),
    source_commit           TEXT,
    source_tree_sha256      TEXT,
    binding_status          TEXT NOT NULL DEFAULT 'BOUND'
                            CHECK (binding_status IN ('BOUND', 'LEGACY_UNBOUND')),
    status                  TEXT NOT NULL DEFAULT 'WAITING_FOR_OPUS',
    request_json_path       TEXT NOT NULL,
    request_markdown_path   TEXT NOT NULL,
    response_path           TEXT,
    payload_json            TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id              TEXT PRIMARY KEY,
    idea_id         TEXT REFERENCES ideas(id),
    venture_id      TEXT REFERENCES ventures(id),
    work_order_id   TEXT REFERENCES work_orders(id),
    run_id          TEXT REFERENCES runs(id),
    external_ref    TEXT,
    kind            TEXT NOT NULL,
    path            TEXT,
    sha256          TEXT,
    trusted         INTEGER NOT NULL DEFAULT 0 CHECK (trusted IN (0, 1)),
    payload_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    created_at      TEXT NOT NULL,
    CHECK (idea_id IS NOT NULL OR venture_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS review_required_changes (
    id                      TEXT PRIMARY KEY,
    review_id               TEXT NOT NULL REFERENCES reviews(id),
    change_id               TEXT NOT NULL,
    description             TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'OPEN'
                            CHECK (status IN ('OPEN', 'SUBMITTED', 'VERIFIED')),
    verified_by_review_id   TEXT REFERENCES reviews(id),
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    UNIQUE (review_id, change_id)
);

CREATE TABLE IF NOT EXISTS review_change_resolutions (
    id                  TEXT PRIMARY KEY,
    review_id           TEXT NOT NULL REFERENCES reviews(id),
    required_change_id  TEXT NOT NULL,
    repair_run_id       TEXT NOT NULL REFERENCES runs(id),
    source_commit       TEXT NOT NULL,
    source_tree_sha256  TEXT NOT NULL,
    evidence_policy_version INTEGER NOT NULL DEFAULT 0,
    evidence_json       TEXT NOT NULL CHECK (json_valid(evidence_json)),
    created_at          TEXT NOT NULL,
    UNIQUE (review_id, required_change_id, repair_run_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    id              TEXT PRIMARY KEY,
    venture_id      TEXT NOT NULL REFERENCES ventures(id),
    work_order_id   TEXT REFERENCES work_orders(id),
    status          TEXT NOT NULL DEFAULT 'PENDING',
    payload_json    TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id              TEXT PRIMARY KEY,
    contract_id     TEXT REFERENCES contracts(id),
    decision_id     TEXT REFERENCES decisions(id),
    status          TEXT NOT NULL,
    actor           TEXT NOT NULL DEFAULT 'CEO',
    payload_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    created_at      TEXT NOT NULL,
    CHECK (contract_id IS NOT NULL OR decision_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id              TEXT PRIMARY KEY,
    venture_id      TEXT NOT NULL REFERENCES ventures(id),
    work_order_id   TEXT REFERENCES work_orders(id),
    run_id          TEXT REFERENCES runs(id),
    path            TEXT NOT NULL,
    sha256          TEXT,
    media_type      TEXT,
    payload_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    created_at      TEXT NOT NULL,
    UNIQUE (venture_id, path)
);

CREATE TABLE IF NOT EXISTS events (
    sequence        INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT NOT NULL UNIQUE,
    event_type      TEXT NOT NULL,
    aggregate_type  TEXT,
    aggregate_id    TEXT,
    venture_id      TEXT REFERENCES ventures(id),
    correlation_id  TEXT,
    payload_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    occurred_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS global_state (
    key             TEXT PRIMARY KEY,
    value_json      TEXT NOT NULL CHECK (json_valid(value_json)),
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    key             TEXT PRIMARY KEY,
    command         TEXT NOT NULL,
    payload_hash    TEXT NOT NULL,
    request_json    TEXT CHECK (request_json IS NULL OR json_valid(request_json)),
    result_json     TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    status          TEXT NOT NULL CHECK (status IN ('CLAIMED', 'COMPLETED')),
    created_at      TEXT NOT NULL,
    completed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_council_responses_idea
    ON council_responses(idea_id);
CREATE INDEX IF NOT EXISTS idx_contracts_idea ON contracts(idea_id);
CREATE INDEX IF NOT EXISTS idx_ventures_idea ON ventures(idea_id);
CREATE INDEX IF NOT EXISTS idx_assumptions_venture ON assumptions(venture_id);
CREATE INDEX IF NOT EXISTS idx_metrics_venture ON metrics(venture_id);
CREATE INDEX IF NOT EXISTS idx_experiments_venture ON experiments(venture_id);
CREATE INDEX IF NOT EXISTS idx_work_orders_venture ON work_orders(venture_id);
CREATE INDEX IF NOT EXISTS idx_runs_work_order ON runs(work_order_id);
CREATE INDEX IF NOT EXISTS idx_reviews_work_order ON reviews(work_order_id);
CREATE INDEX IF NOT EXISTS idx_review_required_changes_review
    ON review_required_changes(review_id);
CREATE INDEX IF NOT EXISTS idx_review_change_resolutions_review
    ON review_change_resolutions(review_id);
CREATE INDEX IF NOT EXISTS idx_evidence_venture ON evidence(venture_id);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(run_id);
CREATE INDEX IF NOT EXISTS idx_decisions_venture ON decisions(venture_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_venture ON artifacts(venture_id);
CREATE INDEX IF NOT EXISTS idx_events_venture ON events(venture_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_aggregate
    ON events(aggregate_type, aggregate_id, sequence);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
"""


_SCHEMA_VERSION = 4

_VERSIONED_INDEXES = r"""
CREATE UNIQUE INDEX IF NOT EXISTS ux_council_active_role
    ON council_responses(idea_id, role)
    WHERE status = 'ACTIVE';
CREATE UNIQUE INDEX IF NOT EXISTS ux_assumption_external_ref
    ON assumptions(venture_id, external_ref)
    WHERE external_ref IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_venture_external_ref
    ON evidence(venture_id, external_ref)
    WHERE venture_id IS NOT NULL AND external_ref IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_idea_external_ref
    ON evidence(idea_id, external_ref)
    WHERE idea_id IS NOT NULL AND external_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_council_responses_idea
    ON council_responses(idea_id);
CREATE INDEX IF NOT EXISTS idx_evidence_venture ON evidence(venture_id);
CREATE INDEX IF NOT EXISTS idx_evidence_idea ON evidence(idea_id);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(run_id);
"""


def utc_now() -> str:
    """Return an unambiguous, lexically sortable UTC timestamp."""

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def new_id(prefix: str) -> str:
    """Create a locally unique, human-readable identifier."""

    if not _IDENTIFIER.fullmatch(prefix):
        raise ValueError(f"invalid id prefix: {prefix!r}")
    return f"{prefix}_{uuid4().hex}"


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def canonical_json(value: JsonValue) -> str:
    """Serialize JSON deterministically for hashes and durable fields."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )


def payload_fingerprint(value: JsonValue) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _create_council_responses_v2(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE council_responses (
            id              TEXT PRIMARY KEY,
            idea_id         TEXT NOT NULL REFERENCES ideas(id),
            role            TEXT NOT NULL CHECK (role IN ('cto', 'cpo', 'cmo')),
            version         INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            status          TEXT NOT NULL DEFAULT 'ACTIVE'
                            CHECK (status IN ('ACTIVE', 'SUPERSEDED')),
            response_hash   TEXT NOT NULL,
            payload_json    TEXT NOT NULL CHECK (json_valid(payload_json)),
            source_path     TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            UNIQUE (idea_id, role, version),
            UNIQUE (idea_id, role, response_hash)
        )
        """
    )


def _create_evidence_v2(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE evidence (
            id              TEXT PRIMARY KEY,
            idea_id         TEXT REFERENCES ideas(id),
            venture_id      TEXT REFERENCES ventures(id),
            work_order_id   TEXT REFERENCES work_orders(id),
            run_id          TEXT REFERENCES runs(id),
            external_ref    TEXT,
            kind            TEXT NOT NULL,
            path            TEXT,
            sha256          TEXT,
            trusted         INTEGER NOT NULL DEFAULT 0 CHECK (trusted IN (0, 1)),
            payload_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
            created_at      TEXT NOT NULL,
            CHECK (idea_id IS NOT NULL OR venture_id IS NOT NULL)
        )
        """
    )


def _migrate_schema(connection: sqlite3.Connection) -> None:
    """Migrate existing durable state without rewriting the append-only ledger."""

    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > _SCHEMA_VERSION:
        raise StorageError(
            f"database schema version {current} is newer than supported "
            f"version {_SCHEMA_VERSION}"
        )

    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        council_columns = _column_names(connection, "council_responses")
        if "version" not in council_columns:
            council_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM council_responses ORDER BY created_at, id"
                ).fetchall()
            ]
            connection.execute("DROP TABLE council_responses")
            _create_council_responses_v2(connection)
            for row in council_rows:
                response = json.loads(row["payload_json"])
                connection.execute(
                    """
                    INSERT INTO council_responses(
                        id, idea_id, role, version, status, response_hash,
                        payload_json, source_path, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, 'ACTIVE', ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["idea_id"],
                        row["role"],
                        payload_fingerprint(response),
                        row["payload_json"],
                        row["source_path"],
                        row["created_at"],
                        row["updated_at"],
                    ),
                )

        contract_columns = _column_names(connection, "contracts")
        if "min_decision_level" not in contract_columns:
            connection.execute(
                "ALTER TABLE contracts ADD COLUMN min_decision_level "
                "TEXT NOT NULL DEFAULT 'FP_LITE'"
            )
            connection.execute(
                "UPDATE contracts SET min_decision_level = decision_level"
            )

        assumption_columns = _column_names(connection, "assumptions")
        if "external_ref" not in assumption_columns:
            connection.execute("ALTER TABLE assumptions ADD COLUMN external_ref TEXT")
            connection.execute("UPDATE assumptions SET external_ref = id")

        review_columns = _column_names(connection, "reviews")
        for statement, column in (
            (
                "ALTER TABLE reviews ADD COLUMN schema_version INTEGER "
                "NOT NULL DEFAULT 1",
                "schema_version",
            ),
            (
                "ALTER TABLE reviews ADD COLUMN parent_review_id TEXT "
                "REFERENCES reviews(id)",
                "parent_review_id",
            ),
            ("ALTER TABLE reviews ADD COLUMN source_commit TEXT", "source_commit"),
            (
                "ALTER TABLE reviews ADD COLUMN source_tree_sha256 TEXT",
                "source_tree_sha256",
            ),
            (
                "ALTER TABLE reviews ADD COLUMN binding_status TEXT NOT NULL "
                "DEFAULT 'LEGACY_UNBOUND' CHECK (binding_status IN "
                "('BOUND', 'LEGACY_UNBOUND'))",
                "binding_status",
            ),
        ):
            if column not in review_columns:
                connection.execute(statement)

        connection.execute(
            """
            UPDATE reviews
            SET binding_status = CASE
                WHEN schema_version >= 2
                     AND source_commit IS NOT NULL
                     AND source_tree_sha256 IS NOT NULL
                THEN 'BOUND'
                ELSE 'LEGACY_UNBOUND'
            END
            """
        )

        evidence_columns = _column_names(connection, "evidence")
        if "idea_id" not in evidence_columns:
            evidence_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT evidence.*, ventures.idea_id AS derived_idea_id
                    FROM evidence
                    LEFT JOIN ventures ON ventures.id = evidence.venture_id
                    ORDER BY evidence.created_at, evidence.id
                    """
                ).fetchall()
            ]
            connection.execute("DROP TABLE evidence")
            _create_evidence_v2(connection)
            for row in evidence_rows:
                payload = json.loads(row["payload_json"])
                external_ref = row["id"] if row["kind"] == "SOURCE_EVIDENCE" else None
                connection.execute(
                    """
                    INSERT INTO evidence(
                        id, idea_id, venture_id, work_order_id, run_id,
                        external_ref, kind, path, sha256, trusted,
                        payload_json, created_at
                    ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["venture_id"],
                        row["work_order_id"],
                        row["run_id"],
                        external_ref,
                        row["kind"],
                        row["path"],
                        row["sha256"],
                        1 if payload.get("trusted") is True else 0,
                        row["payload_json"],
                        row["created_at"],
                    ),
                )

        resolution_columns = _column_names(
            connection,
            "review_change_resolutions",
        )
        if "evidence_policy_version" not in resolution_columns:
            connection.execute(
                "ALTER TABLE review_change_resolutions ADD COLUMN "
                "evidence_policy_version INTEGER NOT NULL DEFAULT 0"
            )

        # Import legacy ReviewResult requirements as explicit OPEN records.
        for review_row in connection.execute(
            "SELECT id, payload_json, created_at, updated_at FROM reviews"
        ).fetchall():
            review_payload = json.loads(review_row["payload_json"])
            result = review_payload.get("result")
            if not isinstance(result, dict):
                continue
            changes = result.get("required_changes")
            if not isinstance(changes, list):
                continue
            for change in changes:
                if not isinstance(change, dict):
                    continue
                change_id = change.get("id")
                description = change.get("description")
                if not isinstance(change_id, str) or not isinstance(description, str):
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO review_required_changes(
                        id, review_id, change_id, description, status,
                        verified_by_review_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'OPEN', NULL, ?, ?)
                    """,
                    (
                        new_id("review_change"),
                        review_row["id"],
                        change_id,
                        description,
                        review_row["created_at"],
                        review_row["updated_at"],
                    ),
                )

        migration_time = utc_now()
        legacy_waiting_work_orders = [
            str(row["work_order_id"])
            for row in connection.execute(
                """
                SELECT DISTINCT work_order_id FROM reviews
                WHERE schema_version < 2 AND status = 'WAITING_FOR_OPUS'
                """
            ).fetchall()
        ]
        connection.execute(
            """
            UPDATE reviews
            SET status = 'SUPERSEDED', updated_at = ?
            WHERE schema_version < 2 AND status = 'WAITING_FOR_OPUS'
            """,
            (migration_time,),
        )
        for work_order_id in legacy_waiting_work_orders:
            connection.execute(
                """
                UPDATE work_orders
                SET status = 'VERIFIED', updated_at = ?
                WHERE id = ? AND status = 'WAITING_FOR_OPUS'
                """,
                (migration_time, work_order_id),
            )
        connection.execute(
            """
            UPDATE reviews
            SET status = 'CHANGES_REQUIRED', updated_at = ?
            WHERE schema_version = 1 AND status = 'CHANGES_APPLIED'
            """,
            (migration_time,),
        )
        connection.execute(
            """
            UPDATE work_orders
            SET status = 'REPAIR_REQUIRED', updated_at = ?
            WHERE status = 'REPAIRED_VERIFIED'
            """,
            (migration_time,),
        )
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")

    connection.executescript(_VERSIONED_INDEXES)


class SQLiteStateStore:
    """Small transactional state store with no external dependencies."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000):
        self.path = Path(path)
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._savepoint_counter = 0

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the configured connection for explicit application SQL."""

        if self._connection is None:
            self.initialize()
        assert self._connection is not None
        return self._connection

    def initialize(self) -> SQLiteStateStore:
        """Open the database, apply safety pragmas, and create the schema."""

        with self._lock:
            if self._connection is not None:
                return self
            if str(self.path) != ":memory:":
                self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(self.path),
                timeout=self.busy_timeout_ms / 1_000,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
                if str(self.path) != ":memory:":
                    connection.execute("PRAGMA journal_mode = WAL").fetchone()
                    connection.execute("PRAGMA synchronous = NORMAL")
                connection.executescript(_SCHEMA)
                _migrate_schema(connection)
                connection.execute(
                    """
                    INSERT INTO global_state(key, value_json, updated_at)
                    VALUES ('stopped', 'false', ?)
                    ON CONFLICT(key) DO NOTHING
                    """,
                    (utc_now(),),
                )
            except BaseException:
                connection.close()
                raise
            self._connection = connection
        return self

    def close(self) -> None:
        with self._lock:
            if self._connection is None:
                return
            if self._connection.in_transaction:
                self._connection.rollback()
            self._connection.close()
            self._connection = None

    def __enter__(self) -> SQLiteStateStore:
        return self.initialize()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Run a transaction, rolling it back on every exceptional exit.

        Nested calls use SQLite savepoints, so storage helpers can safely be
        used inside an application-level atomic transition.
        """

        with self._lock:
            connection = self.connection
            if connection.in_transaction:
                self._savepoint_counter += 1
                name = f"company_os_sp_{self._savepoint_counter}"
                connection.execute(f"SAVEPOINT {name}")
                try:
                    yield connection
                except BaseException:
                    connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    connection.execute(f"RELEASE SAVEPOINT {name}")
                    raise
                else:
                    connection.execute(f"RELEASE SAVEPOINT {name}")
                return

            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def query_one(
        self, sql: str, parameters: Sequence[Any] | Mapping[str, Any] = ()
    ) -> sqlite3.Row | None:
        with self._lock:
            return self.connection.execute(sql, parameters).fetchone()

    def query_all(
        self, sql: str, parameters: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.connection.execute(sql, parameters).fetchall())

    def scalar(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
        *,
        default: Any = None,
    ) -> Any:
        row = self.query_one(sql, parameters)
        return default if row is None else row[0]

    def get_row(self, table: str, record_id: str) -> sqlite3.Row | None:
        table = self._table_name(table)
        key_column = "key" if table in {"global_state", "idempotency"} else "id"
        return self.query_one(
            f'SELECT * FROM "{table}" WHERE "{key_column}" = ?', (record_id,)
        )

    def list_rows(
        self,
        table: str,
        *,
        where: str | None = None,
        parameters: Sequence[Any] = (),
        order_by: str | None = None,
    ) -> list[sqlite3.Row]:
        table = self._table_name(table)
        sql = f'SELECT * FROM "{table}"'
        if where:
            # Callers supply values separately; reject statement chaining.
            if ";" in where:
                raise ValueError("where clause must not contain a semicolon")
            sql += f" WHERE {where}"
        if order_by:
            if not _IDENTIFIER.fullmatch(order_by):
                raise ValueError(f"invalid order_by column: {order_by!r}")
            sql += f' ORDER BY "{order_by}"'
        elif table == "events":
            sql += " ORDER BY sequence"
        return self.query_all(sql, parameters)

    def insert_row(
        self,
        table: str,
        values: Mapping[str, Any],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> sqlite3.Row:
        table = self._table_name(table)
        if not values:
            raise ValueError("values must not be empty")
        columns = [self._column_name(column) for column in values]
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(f'"{column}"' for column in columns)
        sql = f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})'
        params = tuple(values[column] for column in columns)

        def execute(conn: sqlite3.Connection) -> sqlite3.Row:
            cursor = conn.execute(sql, params)
            if table == "events":
                row = conn.execute(
                    "SELECT * FROM events WHERE sequence = ?", (cursor.lastrowid,)
                ).fetchone()
            else:
                key = values.get("id", values.get("key"))
                if key is None:
                    raise ValueError("inserted rows require an id or key")
                row = self._get_row_with_connection(conn, table, str(key))
            assert row is not None
            return row

        if connection is not None:
            return execute(connection)
        with self.transaction() as conn:
            return execute(conn)

    def update_row(
        self,
        table: str,
        record_id: str,
        values: Mapping[str, Any],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> sqlite3.Row:
        table = self._table_name(table)
        if table == "events":
            raise sqlite3.IntegrityError("events are append-only")
        if not values:
            row = self.get_row(table, record_id)
            if row is None:
                raise KeyError(f"{table} record not found: {record_id}")
            return row
        columns = [self._column_name(column) for column in values]
        assignments = ", ".join(f'"{column}" = ?' for column in columns)
        key_column = "key" if table in {"global_state", "idempotency"} else "id"
        sql = f'UPDATE "{table}" SET {assignments} WHERE "{key_column}" = ?'
        params = tuple(values[column] for column in columns) + (record_id,)

        def execute(conn: sqlite3.Connection) -> sqlite3.Row:
            cursor = conn.execute(sql, params)
            if cursor.rowcount != 1:
                raise KeyError(f"{table} record not found: {record_id}")
            row = self._get_row_with_connection(conn, table, record_id)
            assert row is not None
            return row

        if connection is not None:
            return execute(connection)
        with self.transaction() as conn:
            return execute(conn)

    def append_event(
        self,
        event_type: str,
        *,
        payload: JsonValue | None = None,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        venture_id: str | None = None,
        correlation_id: str | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> sqlite3.Row:
        if not event_type.strip():
            raise ValueError("event_type must not be empty")
        values = {
            "id": event_id or new_id("event"),
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "venture_id": venture_id,
            "correlation_id": correlation_id,
            "payload_json": canonical_json({} if payload is None else payload),
            "occurred_at": occurred_at or utc_now(),
        }
        return self.insert_row("events", values, connection=connection)

    def events_for(
        self,
        *,
        venture_id: str | None = None,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        parameters: list[str] = []
        for column, value in (
            ("venture_id", venture_id),
            ("aggregate_type", aggregate_type),
            ("aggregate_id", aggregate_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        return self.list_rows(
            "events",
            where=" AND ".join(clauses) if clauses else None,
            parameters=parameters,
            order_by="sequence",
        )

    def export_events_jsonl(self, destination: str | Path) -> Path:
        """Write a deterministic snapshot of the event ledger atomically."""

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            with self.transaction(immediate=False) as connection:
                rows = connection.execute(
                    "SELECT * FROM events ORDER BY sequence"
                ).fetchall()
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    for row in rows:
                        item = dict(row)
                        item["payload"] = json.loads(item.pop("payload_json"))
                        stream.write(canonical_json(item))
                        stream.write("\n")
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination

    def claim_idempotency(
        self,
        key: str,
        command: str | JsonValue,
        payload: JsonValue = _MISSING,
        *,
        payload_hash: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> IdempotencyClaim:
        """Claim a command, or return its existing result on exact replay.

        Preferred usage is ``claim_idempotency(key, command, payload)``.  The
        two-argument form treats the second argument as payload and uses an
        empty command for compatibility with small callers.  A precomputed
        SHA-256 can be supplied with ``payload_hash=...``.
        """

        if not key:
            raise ValueError("idempotency key must not be empty")
        if payload is _MISSING:
            payload = command
            command = ""
        if not isinstance(command, str):
            raise TypeError("command must be a string")
        request_json = canonical_json(payload)
        fingerprint = payload_hash or hashlib.sha256(
            request_json.encode("utf-8")
        ).hexdigest()
        if not _SHA256.fullmatch(fingerprint):
            raise ValueError("payload_hash must be a lowercase SHA-256 hex digest")

        def execute(conn: sqlite3.Connection) -> IdempotencyClaim:
            now = utc_now()
            cursor = conn.execute(
                """
                INSERT INTO idempotency(
                    key, command, payload_hash, request_json, result_json,
                    status, created_at, completed_at
                )
                VALUES (?, ?, ?, ?, NULL, 'CLAIMED', ?, NULL)
                ON CONFLICT(key) DO NOTHING
                """,
                (key, command, fingerprint, request_json, now),
            )
            row = conn.execute(
                "SELECT * FROM idempotency WHERE key = ?", (key,)
            ).fetchone()
            assert row is not None
            if row["command"] != command or row["payload_hash"] != fingerprint:
                raise IdempotencyConflict(
                    f"idempotency key {key!r} was already used for a different "
                    "command or payload"
                )
            result = (
                None if row["result_json"] is None else json.loads(row["result_json"])
            )
            return IdempotencyClaim(
                key=key,
                command=command,
                payload_hash=fingerprint,
                is_new=cursor.rowcount == 1,
                result=result,
                status=row["status"],
            )

        if connection is not None:
            return execute(connection)
        with self.transaction() as conn:
            return execute(conn)

    def complete_idempotency(
        self,
        key: str,
        result: JsonValue,
        *,
        command: str | None = None,
        payload_hash: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> IdempotencyClaim:
        """Store the immutable result associated with a claimed key."""

        result_json = canonical_json(result)

        def execute(conn: sqlite3.Connection) -> IdempotencyClaim:
            row = conn.execute(
                "SELECT * FROM idempotency WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                raise KeyError(f"idempotency claim not found: {key}")
            if command is not None and row["command"] != command:
                raise IdempotencyConflict(
                    f"idempotency key {key!r} has a different command"
                )
            if payload_hash is not None and row["payload_hash"] != payload_hash:
                raise IdempotencyConflict(
                    f"idempotency key {key!r} has a different payload"
                )
            if row["status"] == "COMPLETED":
                if row["result_json"] != result_json:
                    raise IdempotencyConflict(
                        f"idempotency key {key!r} already has a different result"
                    )
            else:
                conn.execute(
                    """
                    UPDATE idempotency
                    SET result_json = ?, status = 'COMPLETED', completed_at = ?
                    WHERE key = ?
                    """,
                    (result_json, utc_now(), key),
                )
                row = conn.execute(
                    "SELECT * FROM idempotency WHERE key = ?", (key,)
                ).fetchone()
                assert row is not None
            return IdempotencyClaim(
                key=key,
                command=row["command"],
                payload_hash=row["payload_hash"],
                is_new=False,
                result=json.loads(row["result_json"]),
                status=row["status"],
            )

        if connection is not None:
            return execute(connection)
        with self.transaction() as conn:
            return execute(conn)

    def run_idempotent(
        self,
        key: str,
        command: str,
        payload: JsonValue,
        operation: Callable[[sqlite3.Connection], T],
    ) -> T | JsonValue:
        """Atomically claim, execute, and store a JSON-compatible result."""

        with self.transaction() as connection:
            claim = self.claim_idempotency(
                key, command, payload, connection=connection
            )
            if not claim.is_new:
                if not claim.completed:
                    raise IdempotencyInProgress(
                        f"idempotent command is not completed: {key}"
                    )
                return claim.result
            result = operation(connection)
            self.complete_idempotency(key, result, connection=connection)
            return result

    def set_global_state(
        self,
        key: str,
        value: JsonValue,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if not key:
            raise ValueError("global-state key must not be empty")
        encoded = canonical_json(value)

        def execute(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO global_state(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, encoded, utc_now()),
            )

        if connection is not None:
            execute(connection)
        else:
            with self.transaction() as conn:
                execute(conn)

    def get_global_state(self, key: str, default: T | None = None) -> JsonValue | T:
        row = self.query_one(
            "SELECT value_json FROM global_state WHERE key = ?", (key,)
        )
        return default if row is None else json.loads(row["value_json"])

    def set_stopped(
        self,
        stopped: bool,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.set_global_state("stopped", bool(stopped), connection=connection)

    def is_stopped(self) -> bool:
        return bool(self.get_global_state("stopped", False))

    def integrity_check(self) -> str:
        return str(self.scalar("PRAGMA integrity_check"))

    def foreign_keys_enabled(self) -> bool:
        return bool(self.scalar("PRAGMA foreign_keys", default=0))

    def journal_mode(self) -> str:
        return str(self.scalar("PRAGMA journal_mode"))

    @staticmethod
    def _table_name(table: str) -> str:
        if table not in _TABLES:
            raise ValueError(f"unknown storage table: {table!r}")
        return table

    @staticmethod
    def _column_name(column: str) -> str:
        if not _IDENTIFIER.fullmatch(column):
            raise ValueError(f"invalid column name: {column!r}")
        return column

    @staticmethod
    def _get_row_with_connection(
        connection: sqlite3.Connection, table: str, record_id: str
    ) -> sqlite3.Row | None:
        key_column = "key" if table in {"global_state", "idempotency"} else "id"
        return connection.execute(
            f'SELECT * FROM "{table}" WHERE "{key_column}" = ?', (record_id,)
        ).fetchone()


# A concise name for application code; the explicit name remains available to
# make the implementation obvious in diagnostics and tests.
StateStore = SQLiteStateStore


__all__ = [
    "IdempotencyClaim",
    "IdempotencyConflict",
    "IdempotencyInProgress",
    "SQLiteStateStore",
    "StateStore",
    "StorageError",
    "canonical_json",
    "new_id",
    "payload_fingerprint",
    "utc_now",
]
