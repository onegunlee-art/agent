from __future__ import annotations

import json
import re
import shutil
import sqlite3
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    CompanyStoppedError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from .evidence import validate_test_result_receipt
from .council import (
    CouncilMergeConflict,
    council_ignored_fields,
    merge_council_responses,
)
from .first_principles import (
    EvidenceGrant,
    FirstPrinciplesGate,
    GateResult,
    SYNTHETIC_FIXTURE_STATEMENT,
)
from .handoffs import (
    MODEL_METADATA,
    REVIEW_REQUEST_TITLE,
    REVIEWER_METADATA,
    markdown_document,
    review_request_markdown,
    validate_council_response,
    validate_review_result,
)
from .models import (
    CompileOutcome,
    CouncilRequest,
    Evidence,
    ExecutionOutput,
    ExecutorPort,
    Idea,
    Review,
    Run,
    Venture,
    WorkOrder,
)
from .roles import get_role_spec, list_role_specs, serialize_role_spec
from .source_snapshot import (
    GitSourceSnapshot,
    SourceSnapshot,
    SourceSnapshotError,
    SourceSnapshotPort,
)
from .storage import (
    IdempotencyConflict,
    SQLiteStateStore,
    canonical_json,
    new_id,
    utc_now,
)
from .utils import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    contained_path,
    payload_hash,
    read_json,
    sha256_file,
)
from .verifier import (
    result_as_dict,
    run_verifier,
    verifier_hash,
    write_exact_text_verifier,
)


_VENTURE_ID = re.compile(r"^venture_[0-9a-f]{32}$")
_EVIDENCE_EXTERNAL_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ROLES = ("cto", "cpo", "cmo")
_MAX_REGISTERED_EVIDENCE_BYTES = 16 * 1024 * 1024


def _read_bounded_evidence_file(source: Path, *, label: str) -> bytes:
    if source.is_symlink() or not source.is_file():
        raise ValidationError(f"{label} source must be a regular file")
    try:
        if source.stat().st_size > _MAX_REGISTERED_EVIDENCE_BYTES:
            raise ValidationError(f"{label} source exceeds the 16 MiB limit")
        with source.open("rb") as handle:
            content = handle.read(_MAX_REGISTERED_EVIDENCE_BYTES + 1)
    except OSError as exc:
        raise ValidationError(f"{label} source could not be read: {exc}") from exc
    if not content:
        raise ValidationError(f"{label} source must not be empty")
    if len(content) > _MAX_REGISTERED_EVIDENCE_BYTES:
        raise ValidationError(f"{label} source exceeds the 16 MiB limit")
    return content


class ExistingArtifactExecutor:
    """Adapter for an artifact created outside the program by this session."""

    name = "current_cursor_codex_session"

    def execute(self, work_order: WorkOrder, workspace_path: Path) -> ExecutionOutput:
        path = contained_path(workspace_path, work_order.artifact_relative_path)
        if not path.is_file():
            raise ValidationError(f"Expected externally-created artifact is missing: {path}")
        return ExecutionOutput(artifact_path=path)


class CompanyOS:
    def __init__(
        self,
        root: str | Path,
        db_path: str | Path | None = None,
        *,
        source_snapshotter: SourceSnapshotPort | None = None,
        allow_test_reviewers: bool = False,
    ):
        self.root = Path(root).resolve()
        default_db = self.root / "var" / "state" / "company.db"
        self.db_path = Path(db_path).resolve() if db_path else default_db
        self.store = SQLiteStateStore(self.db_path)
        self.gate = FirstPrinciplesGate()
        code_root = Path(__file__).resolve().parents[2]
        self.source_snapshotter = source_snapshotter or GitSourceSnapshot(
            code_root=code_root
        )
        self.allow_test_reviewers = allow_test_reviewers

    def initialize(self) -> CompanyOS:
        self.root.mkdir(parents=True, exist_ok=True)
        self.store.initialize()
        return self

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> CompanyOS:
        return self.initialize()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _relative(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise ValueError(f"Path is outside Company OS root: {resolved}") from exc

    def _absolute(self, stored_path: str) -> Path:
        candidate = Path(stored_path)
        if candidate.is_absolute():
            raise ValueError("Canonical paths must be relative to the Company OS root")
        return contained_path(self.root, candidate)

    def _source_snapshot(self) -> SourceSnapshot:
        try:
            snapshot = self.source_snapshotter.capture()
        except SourceSnapshotError as exc:
            raise ValidationError(f"Source snapshot could not be captured: {exc}") from exc
        if snapshot.dirty:
            raise ValidationError(
                "Source snapshot must be clean before review or repair binding"
            )
        return snapshot

    def _row(self, table: str, record_id: str) -> sqlite3.Row:
        row = self.store.get_row(table, record_id)
        if row is None:
            raise NotFoundError(f"{table} record not found: {record_id}")
        return row

    @staticmethod
    def _json(value: Any) -> str:
        return canonical_json(value)

    def create_idea(self, text: str, *, idempotency_key: str) -> Idea:
        normalized = " ".join(text.strip().splitlines())
        if not normalized:
            raise ValidationError("Idea must not be empty")
        payload = {"text": normalized}

        def operation(connection: sqlite3.Connection) -> dict[str, str]:
            idea_id = new_id("idea")
            now = utc_now()
            self.store.insert_row(
                "ideas",
                {
                    "id": idea_id,
                    "text": normalized,
                    "status": "NEW",
                    "metadata_json": self._json({"input_form": "one_line"}),
                    "created_at": now,
                    "updated_at": now,
                },
                connection=connection,
            )
            self.store.append_event(
                "IDEA_CREATED",
                aggregate_type="Idea",
                aggregate_id=idea_id,
                payload={"text": normalized},
                connection=connection,
            )
            return {"idea_id": idea_id}

        try:
            result = self.store.run_idempotent(
                idempotency_key, "create_idea", payload, operation
            )
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc
        return self.idea(str(result["idea_id"]))

    def idea(self, idea_id: str) -> Idea:
        row = self._row("ideas", idea_id)
        return Idea(id=row["id"], text=row["text"], created_at=row["created_at"])

    def register_idea_evidence(
        self,
        idea_id: str,
        *,
        evidence_file: str | Path,
        external_ref: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Register an immutable CEO-supplied document for one Idea."""

        self.idea(idea_id)
        source = Path(evidence_file)
        content = _read_bounded_evidence_file(source, label="Idea Evidence")
        digest = sha256(content).hexdigest()
        normalized_ref = (
            external_ref.strip()
            if isinstance(external_ref, str)
            else f"ceo-evidence-{digest[:16]}"
        )
        if not _EVIDENCE_EXTERNAL_REF.fullmatch(normalized_ref):
            raise ValidationError(
                "Evidence external_ref must use letters, numbers, '.', '_', ':', or '-'"
            )
        if normalized_ref == "source-evidence-1":
            raise ValidationError("source-evidence-1 is reserved for the system fixture")
        command_payload = {
            "idea_id": idea_id,
            "external_ref": normalized_ref,
            "sha256": digest,
            "size_bytes": len(content),
        }
        created_paths: list[Path] = []

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            duplicate = connection.execute(
                """
                SELECT id FROM evidence
                WHERE idea_id = ? AND (external_ref = ? OR id = ?)
                """,
                (idea_id, normalized_ref, normalized_ref),
            ).fetchone()
            if duplicate is not None:
                raise ValidationError(
                    "Evidence external_ref collides with an existing Evidence "
                    f"alias for this Idea: {normalized_ref}"
                )
            evidence_id = new_id("evidence")
            while evidence_id == normalized_ref or connection.execute(
                """
                SELECT 1 FROM evidence
                WHERE id = ? OR (idea_id = ? AND external_ref = ?)
                """,
                (evidence_id, idea_id, evidence_id),
            ).fetchone() is not None:
                evidence_id = new_id("evidence")
            target = contained_path(
                self.root,
                "var",
                "evidence",
                "ideas",
                idea_id,
                evidence_id,
                "source.txt",
            )
            atomic_write_bytes(target, content)
            created_paths.append(target)
            copied_digest = sha256_file(target)
            if copied_digest != digest:
                raise ValidationError("Registered Evidence copy hash mismatch")
            now = utc_now()
            self.store.insert_row(
                "evidence",
                {
                    "id": evidence_id,
                    "idea_id": idea_id,
                    "venture_id": None,
                    "work_order_id": None,
                    "run_id": None,
                    "external_ref": normalized_ref,
                    "kind": "CEO_SUPPLIED_DOCUMENT",
                    "path": self._relative(target),
                    "sha256": copied_digest,
                    "trusted": 1,
                    "payload_json": self._json(
                        {
                            "schema_version": 1,
                            "trusted": True,
                            "claimed_actor": "CEO",
                            "trust_basis": "CEO_EXPLICIT_REGISTRATION",
                            "source_type": "USER_SUPPLIED_DOCUMENT",
                            "claim_scope": "GENERAL_DOCUMENT",
                            "size_bytes": len(content),
                        }
                    ),
                    "created_at": now,
                },
                connection=connection,
            )
            self.store.append_event(
                "IDEA_EVIDENCE_REGISTERED",
                aggregate_type="Evidence",
                aggregate_id=evidence_id,
                payload={
                    "idea_id": idea_id,
                    "external_ref": normalized_ref,
                    "sha256": copied_digest,
                    "claimed_actor": "CEO",
                    "actor_identity_verified": False,
                },
                connection=connection,
            )
            return {
                "id": evidence_id,
                "idea_id": idea_id,
                "external_ref": normalized_ref,
                "kind": "CEO_SUPPLIED_DOCUMENT",
                "path": self._relative(target),
                "sha256": copied_digest,
            }

        try:
            result = self.store.run_idempotent(
                idempotency_key,
                "register_idea_evidence",
                command_payload,
                operation,
            )
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc
        except BaseException:
            for path in created_paths:
                if path.is_file():
                    path.unlink()
                parent = path.parent
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            raise
        return dict(result)

    def register_test_result(
        self,
        work_order_id: str,
        *,
        required_change_id: str,
        result_file: str | Path,
        test_node_ids: Iterable[str],
        idempotency_key: str,
        source_commit: str,
        review_id: str | None = None,
    ) -> dict[str, Any]:
        """Register immutable, change-specific local test execution evidence."""

        if not isinstance(required_change_id, str):
            raise ValidationError("TEST_RESULT required_change_id must be text")
        normalized_change_id = required_change_id.strip()
        if not normalized_change_id:
            raise ValidationError("TEST_RESULT required_change_id must not be empty")
        nodes = [
            node.strip()
            for node in test_node_ids
            if isinstance(node, str) and node.strip()
        ]
        if not nodes:
            raise ValidationError("TEST_RESULT requires at least one test node id")
        if len(nodes) != len(set(nodes)):
            raise ValidationError("TEST_RESULT test node ids must be unique")
        nodes = sorted(nodes)
        source = Path(result_file)
        content = _read_bounded_evidence_file(source, label="TEST_RESULT")
        digest = sha256(content).hexdigest()
        receipt = validate_test_result_receipt(
            content,
            selected_node_ids=nodes,
            source_commit=source_commit,
            source_tree_sha256=None,
        )
        declared_tree = str(receipt["source_tree_sha256"])
        existing = self.store.get_row("idempotency", idempotency_key)
        if existing is not None and existing["status"] == "COMPLETED":
            if existing["command"] != "register_test_result":
                raise ConflictError(
                    "Idempotency key belongs to a different command"
                )
            stored_request = json.loads(existing["request_json"])
            replay_identity = {
                "work_order_id": work_order_id,
                "required_change_id": normalized_change_id,
                "test_node_ids": nodes,
                "result_file_sha256": digest,
                "source_commit": source_commit,
                "source_tree_sha256": declared_tree,
            }
            stored_identity = {
                key: stored_request.get(key) for key in replay_identity
            }
            if stored_identity != replay_identity or (
                review_id is not None
                and stored_request.get("review_id") != review_id
            ):
                raise ConflictError(
                    "Idempotency key was reused with different TEST_RESULT inputs"
                )
            return dict(json.loads(existing["result_json"]))

        work = self.work_order(work_order_id)
        if work.status != "REPAIR_REQUIRED":
            raise ValidationError("TEST_RESULT requires a REPAIR_REQUIRED WorkOrder")
        snapshot = self._source_snapshot()
        if source_commit != snapshot.source_commit:
            raise ValidationError("TEST_RESULT source_commit does not match source")
        if declared_tree != snapshot.source_tree_sha256:
            raise ValidationError(
                "TEST_RESULT receipt source_tree_sha256 does not match source"
            )
        if review_id is None:
            review_row = self.store.query_one(
                """
                SELECT * FROM reviews
                WHERE work_order_id = ? AND status = 'CHANGES_REQUIRED'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (work_order_id,),
            )
        else:
            review_row = self.store.query_one(
                """
                SELECT * FROM reviews
                WHERE id = ? AND work_order_id = ? AND status = 'CHANGES_REQUIRED'
                """,
                (review_id, work_order_id),
            )
        if review_row is None:
            raise ValidationError(
                "TEST_RESULT must reference the latest CHANGES_REQUIRED Review"
            )
        latest_review = self.store.query_one(
            """
            SELECT id FROM reviews
            WHERE work_order_id = ? AND status = 'CHANGES_REQUIRED'
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (work_order_id,),
        )
        if latest_review is None or latest_review["id"] != review_row["id"]:
            raise ValidationError(
                "TEST_RESULT must reference the latest CHANGES_REQUIRED Review"
            )
        change_row = self.store.query_one(
            """
            SELECT status FROM review_required_changes
            WHERE review_id = ? AND change_id = ?
            """,
            (review_row["id"], normalized_change_id),
        )
        if change_row is None or change_row["status"] != "OPEN":
            raise ValidationError("TEST_RESULT must reference an OPEN required_change")
        selected_review_id = str(review_row["id"])
        command_payload = {
            "work_order_id": work_order_id,
            "review_id": selected_review_id,
            "required_change_id": normalized_change_id,
            "test_node_ids": nodes,
            "result_file_sha256": digest,
            "source_commit": source_commit,
            "source_tree_sha256": declared_tree,
        }
        created_paths: list[Path] = []

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            current_snapshot = self._source_snapshot()
            if current_snapshot != snapshot:
                raise ValidationError(
                    "Source changed while registering TEST_RESULT Evidence"
                )
            current_work = connection.execute(
                "SELECT status, venture_id FROM work_orders WHERE id = ?",
                (work_order_id,),
            ).fetchone()
            current_change = connection.execute(
                """
                SELECT status FROM review_required_changes
                WHERE review_id = ? AND change_id = ?
                """,
                (selected_review_id, normalized_change_id),
            ).fetchone()
            if current_work is None or current_work["status"] != "REPAIR_REQUIRED":
                raise ValidationError(
                    "TEST_RESULT requires a REPAIR_REQUIRED WorkOrder"
                )
            if current_change is None or current_change["status"] != "OPEN":
                raise ValidationError(
                    "TEST_RESULT must reference an OPEN required_change"
                )
            evidence_id = new_id("evidence")
            target = contained_path(
                self.root,
                "var",
                "evidence",
                "test_results",
                work_order_id,
                selected_review_id,
                payload_hash(normalized_change_id)[:16],
                f"{evidence_id}.json",
            )
            atomic_write_bytes(target, content)
            created_paths.append(target)
            copied_digest = sha256_file(target)
            if copied_digest != digest:
                raise ValidationError("TEST_RESULT Evidence copy hash mismatch")
            now = utc_now()
            payload = {
                "schema_version": 1,
                "trusted": True,
                "trust_basis": "LOCAL_TEST_RESULT_REGISTRATION",
                "review_id": selected_review_id,
                "required_change_id": normalized_change_id,
                "test_node_ids": nodes,
                "result_file_sha256": copied_digest,
                "source_commit": snapshot.source_commit,
                "source_tree_sha256": snapshot.source_tree_sha256,
                "receipt_kind": receipt["kind"],
                "receipt_status": receipt["status"],
                "receipt_exit_code": receipt["exit_code"],
            }
            self.store.insert_row(
                "evidence",
                {
                    "id": evidence_id,
                    "idea_id": None,
                    "venture_id": current_work["venture_id"],
                    "work_order_id": work_order_id,
                    "run_id": None,
                    "external_ref": None,
                    "kind": "TEST_RESULT",
                    "path": self._relative(target),
                    "sha256": copied_digest,
                    "trusted": 1,
                    "payload_json": self._json(payload),
                    "created_at": now,
                },
                connection=connection,
            )
            self.store.append_event(
                "TEST_RESULT_EVIDENCE_REGISTERED",
                aggregate_type="Evidence",
                aggregate_id=evidence_id,
                venture_id=str(current_work["venture_id"]),
                payload={
                    "work_order_id": work_order_id,
                    "review_id": selected_review_id,
                    "required_change_id": normalized_change_id,
                    "test_node_ids": nodes,
                    "result_file_sha256": copied_digest,
                    "source_commit": snapshot.source_commit,
                    "source_tree_sha256": snapshot.source_tree_sha256,
                },
                connection=connection,
            )
            return {
                "id": evidence_id,
                "kind": "TEST_RESULT",
                "work_order_id": work_order_id,
                "review_id": selected_review_id,
                "required_change_id": normalized_change_id,
                "test_node_ids": nodes,
                "path": self._relative(target),
                "sha256": copied_digest,
                "source_commit": snapshot.source_commit,
                "source_tree_sha256": snapshot.source_tree_sha256,
            }

        try:
            result = self.store.run_idempotent(
                idempotency_key,
                "register_test_result",
                command_payload,
                operation,
            )
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc
        except BaseException:
            for path in created_paths:
                if path.is_file():
                    path.unlink()
                parent = path.parent
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            raise
        return dict(result)

    def prepare_council(self, idea_id: str) -> tuple[CouncilRequest, ...]:
        idea = self.idea(idea_id)
        registered_inputs = [
            {
                "id": row["id"],
                "external_ref": row["external_ref"],
                "sha256": row["sha256"],
            }
            for row in self.store.query_all(
                """
                SELECT id, external_ref, sha256 FROM evidence
                WHERE idea_id = ? AND trusted = 1
                  AND kind != 'SYNTHETIC_FIXTURE'
                ORDER BY id
                """,
                (idea_id,),
            )
        ]
        command_payload = {
            "idea_id": idea_id,
            "schema_version": 1,
            "registered_evidence": registered_inputs,
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            directory = contained_path(
                self.root, "var", "handoffs", "council", idea_id
            )
            source_evidence_path = directory / "idea_source_evidence.json"
            source_evidence_ref = "source-evidence-1"
            source_evidence = {
                "schema_version": 1,
                "idea_id": idea.id,
                "kind": "SYNTHETIC_FIXTURE",
                "statement": SYNTHETIC_FIXTURE_STATEMENT,
                "idea_text": idea.text,
            }
            now = utc_now()
            existing_source = connection.execute(
                """
                SELECT id, kind, path, sha256 FROM evidence
                WHERE idea_id = ? AND external_ref = ?
                """,
                (idea_id, source_evidence_ref),
            ).fetchone()
            if existing_source is None:
                atomic_write_json(source_evidence_path, source_evidence)
                source_evidence_id = new_id("evidence")
                while connection.execute(
                    """
                    SELECT 1 FROM evidence
                    WHERE id = ? OR (idea_id = ? AND external_ref = ?)
                    """,
                    (source_evidence_id, idea_id, source_evidence_id),
                ).fetchone() is not None:
                    source_evidence_id = new_id("evidence")
                self.store.insert_row(
                    "evidence",
                    {
                        "id": source_evidence_id,
                        "idea_id": idea_id,
                        "venture_id": None,
                        "work_order_id": None,
                        "run_id": None,
                        "external_ref": source_evidence_ref,
                        "kind": "SYNTHETIC_FIXTURE",
                        "path": self._relative(source_evidence_path),
                        "sha256": sha256_file(source_evidence_path),
                        "trusted": 1,
                        "payload_json": self._json(
                            {
                                "schema_version": 1,
                                "trusted": True,
                                "trust_basis": "SYSTEM_SYNTHETIC_FIXTURE",
                                "source_type": "SYNTHETIC_FIXTURE",
                                "claim_scope": "EXACT_STATEMENT",
                                "supported_statements": [
                                    SYNTHETIC_FIXTURE_STATEMENT
                                ],
                            }
                        ),
                        "created_at": now,
                    },
                    connection=connection,
                )
            else:
                if existing_source["kind"] != "SYNTHETIC_FIXTURE":
                    raise ValidationError(
                        "Reserved source-evidence-1 has an invalid Evidence kind"
                    )
                issue = self._file_integrity_issue(
                    source_table="evidence",
                    record_id=str(existing_source["id"]),
                    stored_path=existing_source["path"],
                    expected_sha256=existing_source["sha256"],
                )
                if issue is not None:
                    raise ValidationError(
                        "Existing synthetic fixture Evidence is missing or changed"
                    )
            available_evidence = [
                {
                    "id": row["id"],
                    "external_ref": row["external_ref"],
                    "kind": row["kind"],
                    "path": row["path"],
                    "sha256": row["sha256"],
                }
                for row in connection.execute(
                    """
                    SELECT id, external_ref, kind, path, sha256
                    FROM evidence
                    WHERE idea_id = ? AND trusted = 1
                    ORDER BY created_at, id
                    """,
                    (idea_id,),
                ).fetchall()
            ]
            request_rows: list[dict[str, str]] = []
            for role in _ROLES:
                spec = serialize_role_spec(role)
                request = {
                    "schema_version": 1,
                    "request_id": f"{idea_id}:{role}:request:v1",
                    "idea": {"id": idea.id, "text": idea.text},
                    "role": role,
                    "role_spec": spec,
                    "model_metadata": MODEL_METADATA,
                    "execution_status": "NOT_EXECUTED",
                    "available_evidence": available_evidence,
                    "response_requirements": {
                        "required_envelope_fields": [
                            "schema_version",
                            "idea_id",
                            "role",
                            "model_metadata",
                            "outputs",
                            "contract_contribution",
                        ],
                        "required_output_fields": list(
                            get_role_spec(role).required_structured_outputs
                        ),
                    },
                }
                json_path = directory / f"{role}_request.json"
                markdown_path = directory / f"{role}_request.md"
                atomic_write_json(json_path, request)
                atomic_write_text(
                    markdown_path,
                    markdown_document(f"{role.upper()} Review Request", request),
                )
                request_rows.append(
                    {
                        "role": role,
                        "json_path": self._relative(json_path),
                        "markdown_path": self._relative(markdown_path),
                    }
                )
            connection.execute(
                "UPDATE ideas SET status = 'COUNCIL_PREPARED', updated_at = ? WHERE id = ?",
                (now, idea_id),
            )
            self.store.append_event(
                "COUNCIL_REQUESTS_PREPARED",
                aggregate_type="Idea",
                aggregate_id=idea_id,
                payload={"roles": list(_ROLES)},
                connection=connection,
            )
            return {"requests": request_rows}

        result = self.store.run_idempotent(
            f"council-prepare:{idea_id}:v2:{payload_hash(registered_inputs)}",
            "prepare_council",
            command_payload,
            operation,
        )
        return tuple(
            CouncilRequest(
                role=item["role"],
                json_path=self._absolute(item["json_path"]),
                markdown_path=self._absolute(item["markdown_path"]),
            )
            for item in result["requests"]
        )

    def ingest_council_response(
        self,
        idea_id: str,
        *,
        role: str,
        response_file: str | Path,
    ) -> Path:
        self.idea(idea_id)
        normalized_role = role.strip().lower()
        if normalized_role not in _ROLES:
            raise ValidationError(f"Unknown executive role: {role}")
        source = Path(response_file).resolve()
        payload = read_json(source)
        if not isinstance(payload, dict):
            raise ValidationError("Council response must be a JSON object")
        validate_council_response(
            payload,
            idea_id=idea_id,
            role=normalized_role,
            required_outputs=get_role_spec(normalized_role).required_structured_outputs,
        )
        response_digest = payload_hash(payload)
        target = contained_path(
            self.root,
            "var",
            "handoffs",
            "council",
            idea_id,
            f"{normalized_role}_response_{response_digest}.json",
        )
        command_payload = {
            "idea_id": idea_id,
            "role": normalized_role,
            "response_hash": response_digest,
        }

        active_before = self.store.query_one(
            """
            SELECT id, response_hash, updated_at
            FROM council_responses
            WHERE idea_id = ? AND role = ? AND status = 'ACTIVE'
            """,
            (idea_id, normalized_role),
        )
        activation_context = (
            f"{active_before['id']}:{active_before['updated_at']}"
            if active_before is not None
            else "none"
        )

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            now = utc_now()
            existing_response = connection.execute(
                """
                SELECT id, version, status, source_path
                FROM council_responses
                WHERE idea_id = ? AND role = ? AND response_hash = ?
                """,
                (idea_id, normalized_role, response_digest),
            ).fetchone()
            if (
                existing_response is not None
                and existing_response["status"] == "ACTIVE"
            ):
                return {
                    "response_path": existing_response["source_path"],
                    "response_id": existing_response["id"],
                    "version": existing_response["version"],
                }

            next_version = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) + 1
                    FROM council_responses WHERE idea_id = ? AND role = ?
                    """,
                    (idea_id, normalized_role),
                ).fetchone()[0]
            )
            connection.execute(
                """
                UPDATE council_responses
                SET status = 'SUPERSEDED', updated_at = ?
                WHERE idea_id = ? AND role = ? AND status = 'ACTIVE'
                """,
                (now, idea_id, normalized_role),
            )
            if existing_response is None:
                response_id = new_id("council_response")
                response_version = next_version
                event_type = "COUNCIL_RESPONSE_INGESTED"
                self.store.insert_row(
                    "council_responses",
                    {
                        "id": response_id,
                        "idea_id": idea_id,
                        "role": normalized_role,
                        "version": response_version,
                        "status": "ACTIVE",
                        "response_hash": response_digest,
                        "payload_json": self._json(payload),
                        "source_path": self._relative(target),
                        "created_at": now,
                        "updated_at": now,
                    },
                    connection=connection,
                )
                self.store.insert_row(
                    "evidence",
                    {
                        "id": new_id("evidence"),
                        "idea_id": idea_id,
                        "venture_id": None,
                        "work_order_id": None,
                        "run_id": None,
                        "external_ref": (
                            f"model-output:{normalized_role}:{response_digest}"
                        ),
                        "kind": "MODEL_OUTPUT",
                        "path": self._relative(target),
                        "sha256": sha256_file(target),
                        "trusted": 0,
                        "payload_json": self._json(
                            {
                                "trusted": False,
                                "role": normalized_role,
                                "response_hash": response_digest,
                            }
                        ),
                        "created_at": now,
                    },
                    connection=connection,
                )
            else:
                response_id = str(existing_response["id"])
                response_version = int(existing_response["version"])
                event_type = "COUNCIL_RESPONSE_REACTIVATED"
                connection.execute(
                    """
                    UPDATE council_responses
                    SET status = 'ACTIVE', source_path = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (self._relative(target), now, response_id),
                )
            self.store.append_event(
                event_type,
                aggregate_type="Idea",
                aggregate_id=idea_id,
                payload={
                    "role": normalized_role,
                    "response_hash": command_payload["response_hash"],
                    "response_id": response_id,
                    "version": response_version,
                },
                connection=connection,
            )
            return {
                "response_path": self._relative(target),
                "response_id": response_id,
                "version": response_version,
            }

        target_existed = target.exists()
        atomic_write_json(target, payload)
        try:
            result = self.store.run_idempotent(
                (
                    f"council-ingest:{idea_id}:{normalized_role}:"
                    f"{activation_context}:{response_digest}"
                ),
                "ingest_council_response",
                command_payload,
                operation,
            )
        except IdempotencyConflict as exc:
            if not target_existed and target.exists():
                target.unlink()
            raise ConflictError(str(exc)) from exc
        except BaseException:
            if not target_existed and target.exists():
                target.unlink()
            raise
        return self._absolute(str(result["response_path"]))

    def compile_council(
        self,
        idea_id: str,
        *,
        idempotency_key: str,
        min_decision_level: str = "FP_STANDARD",
    ) -> CompileOutcome:
        self.idea(idea_id)
        rows = self.store.query_all(
            """
            SELECT * FROM council_responses
            WHERE idea_id = ? AND status = 'ACTIVE'
            ORDER BY role
            """,
            (idea_id,),
        )
        by_role = {row["role"]: json.loads(row["payload_json"]) for row in rows}
        missing = [role for role in _ROLES if role not in by_role]
        if missing:
            raise ValidationError(
                f"Council responses are incomplete; missing: {', '.join(missing)}"
            )
        response_metadata = {
            row["role"]: {
                "response_id": row["id"],
                "version": row["version"],
                "response_hash": row["response_hash"],
                "source_path": row["source_path"],
            }
            for row in rows
        }
        try:
            contract = merge_council_responses(
                by_role,
                response_metadata=response_metadata,
            )
        except CouncilMergeConflict as exc:
            fingerprints = {
                role: response_metadata[role]["response_hash"] for role in _ROLES
            }
            inbox_path = contained_path(
                self.root, "var", "inbox", "ideas", idea_id, "council_conflict.json"
            )
            conflict_payload = {
                "type": "COUNCIL_SHARED_FIELD_CONFLICT",
                "idea_id": idea_id,
                "conflicts": exc.conflicts,
                "role_response_hashes": fingerprints,
                "status": "CEO_DECISION_REQUIRED",
            }
            conflict_hash = payload_hash(conflict_payload)
            prior_hash = None
            if inbox_path.is_file():
                try:
                    prior = read_json(inbox_path)
                    if isinstance(prior, dict):
                        prior_hash = prior.get("conflict_hash")
                except (OSError, UnicodeError, json.JSONDecodeError):
                    prior_hash = None
            atomic_write_json(
                inbox_path,
                {**conflict_payload, "conflict_hash": conflict_hash},
            )
            if prior_hash != conflict_hash:
                self.store.append_event(
                    "COUNCIL_CONFLICT_DETECTED",
                    aggregate_type="Idea",
                    aggregate_id=idea_id,
                    payload={
                        "inbox_path": self._relative(inbox_path),
                        "hashes": fingerprints,
                        "conflicts": exc.conflicts,
                        "conflict_hash": conflict_hash,
                    },
                )
            raise ConflictError(f"Council contributions conflict; see {inbox_path}")

        evidence_grants = self._evidence_grants(idea_id)
        gate_result = self.gate.validate(
            contract,
            min_decision_level=min_decision_level,
            trusted_evidence_refs=frozenset(evidence_grants),
            evidence_grants=evidence_grants,
            ceo_approved=False,
        )
        command_payload = {
            "idea_id": idea_id,
            "min_decision_level": min_decision_level,
            "response_hashes": {
                role: response_metadata[role]["response_hash"] for role in _ROLES
            },
            "evidence_grants_fingerprint": self._evidence_grants_fingerprint(
                evidence_grants
            ),
            "gate_result_fingerprint": self._gate_result_fingerprint(gate_result),
        }
        outcome = self._persist_contract(
            idea_id=idea_id,
            contract=contract,
            min_decision_level=min_decision_level,
            gate_result=gate_result,
            idempotency_key=idempotency_key,
            command="compile_council",
            command_payload=command_payload,
        )
        self._close_shared_council_conflict_after_resubmission(
            idea_id,
            contract_id=outcome.contract_id,
        )
        self._surface_council_non_owner_conflicts(
            idea_id,
            contract,
            by_role,
            contract_id=outcome.contract_id,
        )
        return outcome

    def resolve_council(
        self,
        idea_id: str,
        *,
        contract_file: str | Path,
        min_decision_level: str = "FP_STANDARD",
        idempotency_key: str,
    ) -> CompileOutcome:
        self.idea(idea_id)
        supplied = read_json(Path(contract_file).resolve())
        if not isinstance(supplied, dict):
            raise ValidationError("Resolved VentureContract must be a JSON object")
        contract = deepcopy(supplied)
        contract.pop("council_provenance", None)
        contract.pop("executive_outputs", None)
        rows = self.store.query_all(
            """
            SELECT * FROM council_responses
            WHERE idea_id = ? AND status = 'ACTIVE' ORDER BY role
            """,
            (idea_id,),
        )
        by_role = {row["role"]: json.loads(row["payload_json"]) for row in rows}
        missing = [role for role in _ROLES if role not in by_role]
        if missing:
            raise ValidationError(
                f"Council responses are incomplete; missing: {', '.join(missing)}"
            )
        response_hashes = {row["role"]: row["response_hash"] for row in rows}
        response_metadata = {
            row["role"]: {
                "response_id": row["id"],
                "version": row["version"],
                "response_hash": row["response_hash"],
                "source_path": row["source_path"],
                "contribution_hash": payload_hash(
                    by_role[row["role"]]["contract_contribution"]
                ),
                "outputs_hash": payload_hash(by_role[row["role"]]["outputs"]),
            }
            for row in rows
        }
        contract["executive_outputs"] = {
            role: deepcopy(by_role[role]["outputs"]) for role in _ROLES
        }
        contributions = {
            role: by_role[role]["contract_contribution"] for role in _ROLES
        }
        ignored_fields, non_owner_conflicts = council_ignored_fields(contributions)
        contract["council_provenance"] = {
            "compiler": "ceo_conflict_resolution_v2",
            "roles": list(_ROLES),
            "response_hashes": response_hashes,
            "responses": response_metadata,
            "resolution_source": "user_supplied_complete_contract",
            "ignored_fields": ignored_fields,
            "non_owner_conflicts": non_owner_conflicts,
            "agreement_is_not_evidence": True,
        }
        evidence_grants = self._evidence_grants(idea_id)
        gate_result = self.gate.validate(
            contract,
            min_decision_level=min_decision_level,
            trusted_evidence_refs=frozenset(evidence_grants),
            evidence_grants=evidence_grants,
            ceo_approved=False,
        )
        command_payload = {
            "idea_id": idea_id,
            "min_decision_level": min_decision_level,
            "contract_hash": payload_hash(contract),
            "response_hashes": response_hashes,
            "evidence_grants_fingerprint": self._evidence_grants_fingerprint(
                evidence_grants
            ),
            "gate_result_fingerprint": self._gate_result_fingerprint(gate_result),
        }
        outcome = self._persist_contract(
            idea_id=idea_id,
            contract=contract,
            min_decision_level=min_decision_level,
            gate_result=gate_result,
            idempotency_key=idempotency_key,
            command="resolve_council",
            command_payload=command_payload,
            resolved=True,
        )
        inbox_path = contained_path(
            self.root, "var", "inbox", "ideas", idea_id, "council_conflict.json"
        )
        atomic_write_json(
            inbox_path,
            {
                "type": "COUNCIL_SHARED_FIELD_CONFLICT",
                "idea_id": idea_id,
                "status": "RESOLVED",
                "contract_id": outcome.contract_id,
            },
        )
        self._surface_council_non_owner_conflicts(
            idea_id,
            contract,
            by_role,
            contract_id=outcome.contract_id,
            resolved_by_ceo=True,
        )
        return outcome

    def _close_shared_council_conflict_after_resubmission(
        self,
        idea_id: str,
        *,
        contract_id: str,
    ) -> None:
        """Close a durable shared-field conflict after responses converge."""

        inbox_path = contained_path(
            self.root, "var", "inbox", "ideas", idea_id, "council_conflict.json"
        )
        if not inbox_path.is_file():
            return
        try:
            prior = read_json(inbox_path)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if (
            not isinstance(prior, dict)
            or prior.get("type") != "COUNCIL_SHARED_FIELD_CONFLICT"
            or prior.get("status") != "CEO_DECISION_REQUIRED"
        ):
            return
        core = {
            "type": "COUNCIL_SHARED_FIELD_CONFLICT",
            "idea_id": idea_id,
            "status": "RESOLVED_BY_ROLE_RESUBMISSION",
            "contract_id": contract_id,
            "resolution_method": "ROLE_RESPONSE_RESUBMISSION",
            "previous_conflict_hash": prior.get("conflict_hash"),
        }
        resolution_hash = payload_hash(core)
        atomic_write_json(
            inbox_path,
            {**core, "conflict_hash": resolution_hash},
        )
        self.store.append_event(
            "COUNCIL_CONFLICT_RESOLVED",
            aggregate_type="Idea",
            aggregate_id=idea_id,
            payload={
                "inbox_path": self._relative(inbox_path),
                "contract_id": contract_id,
                "resolution_method": "ROLE_RESPONSE_RESUBMISSION",
                "previous_conflict_hash": prior.get("conflict_hash"),
                "resolution_hash": resolution_hash,
            },
        )

    def _surface_council_non_owner_conflicts(
        self,
        idea_id: str,
        contract: dict[str, Any],
        responses: dict[str, dict[str, Any]],
        *,
        contract_id: str | None = None,
        resolved_by_ceo: bool = False,
    ) -> None:
        provenance = contract.get("council_provenance", {})
        conflict_records = provenance.get("non_owner_conflicts", [])
        if not isinstance(conflict_records, list):
            return
        inbox_path = contained_path(
            self.root,
            "var",
            "inbox",
            "ideas",
            idea_id,
            "council_non_owner_conflicts.json",
        )
        if not conflict_records and not inbox_path.is_file():
            return
        response_metadata = provenance.get("responses", {})
        expanded: list[dict[str, Any]] = []
        for item in conflict_records:
            if not isinstance(item, dict):
                continue
            field = item.get("field")
            owner_role = item.get("owner_role")
            opinion_role = item.get("opinion_role")
            if (
                not isinstance(field, str)
                or owner_role not in responses
                or opinion_role not in responses
            ):
                continue
            owner_contribution = responses[owner_role].get(
                "contract_contribution", {}
            )
            opinion_contribution = responses[opinion_role].get(
                "contract_contribution", {}
            )
            expanded.append(
                {
                    **item,
                    "owner_value": deepcopy(owner_contribution.get(field)),
                    "opinion_value": deepcopy(opinion_contribution.get(field)),
                    "owner_source_path": (
                        response_metadata.get(owner_role, {}).get("source_path")
                        if isinstance(response_metadata, dict)
                        else None
                    ),
                    "opinion_source_path": (
                        response_metadata.get(opinion_role, {}).get("source_path")
                        if isinstance(response_metadata, dict)
                        else None
                    ),
                    "selected_contract_value": deepcopy(contract.get(field)),
                    "selected_contract_value_hash": payload_hash(
                        contract.get(field)
                    ),
                }
            )
        status = (
            "RESOLVED_BY_CEO"
            if expanded and resolved_by_ceo
            else "CEO_REVIEW_REQUIRED"
            if expanded
            else "RESOLVED"
        )
        core = {
            "type": "COUNCIL_NON_OWNER_CONFLICT",
            "idea_id": idea_id,
            "status": status,
            "contract_id": contract_id,
            "conflicts": expanded,
        }
        conflict_hash = payload_hash(core)
        prior_hash = None
        if inbox_path.is_file():
            try:
                prior = read_json(inbox_path)
                if isinstance(prior, dict):
                    prior_hash = prior.get("conflict_hash")
            except (OSError, UnicodeError, json.JSONDecodeError):
                prior_hash = None
        atomic_write_json(inbox_path, {**core, "conflict_hash": conflict_hash})
        if prior_hash != conflict_hash:
            self.store.append_event(
                (
                    "COUNCIL_NON_OWNER_CONFLICT_DETECTED"
                    if status == "CEO_REVIEW_REQUIRED"
                    else "COUNCIL_NON_OWNER_CONFLICT_RESOLVED"
                ),
                aggregate_type="Idea",
                aggregate_id=idea_id,
                payload={
                    "inbox_path": self._relative(inbox_path),
                    "conflict_hash": conflict_hash,
                    "status": status,
                    "contract_id": contract_id,
                    "resolved_by_ceo": resolved_by_ceo,
                    "conflicts": conflict_records,
                },
            )

    def _trusted_evidence_refs(
        self,
        idea_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> frozenset[str]:
        return frozenset(self._evidence_grants(idea_id, connection=connection))

    def idea_evidence_fingerprint(self, idea_id: str) -> str:
        """Fingerprint the currently usable Evidence grants for CLI replay keys."""

        self.idea(idea_id)
        return self._evidence_grants_fingerprint(self._evidence_grants(idea_id))

    @staticmethod
    def _evidence_grants_fingerprint(
        grants: dict[str, EvidenceGrant],
    ) -> str:
        return payload_hash(
            {
                reference: {
                    "kind": grant.kind,
                    "source_type": grant.source_type,
                    "claim_scope": grant.claim_scope,
                    "supported_statements": list(grant.supported_statements),
                    "trusted": grant.trusted,
                }
                for reference, grant in grants.items()
            }
        )

    @staticmethod
    def _gate_result_fingerprint(gate_result: GateResult) -> str:
        return payload_hash(
            {
                "passed": gate_result.passed,
                "violations": [
                    {
                        "code": violation.code,
                        "field": violation.field,
                        "message": violation.message,
                    }
                    for violation in gate_result.violations
                ],
            }
        )

    def _evidence_grants(
        self,
        idea_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, EvidenceGrant]:
        query = """
            SELECT id, external_ref, kind, path, sha256, trusted, payload_json
            FROM evidence
            WHERE idea_id = ? AND trusted = 1
            ORDER BY created_at, id
        """
        rows = (
            connection.execute(query, (idea_id,)).fetchall()
            if connection is not None
            else self.store.query_all(query, (idea_id,))
        )
        grants: dict[str, EvidenceGrant] = {}
        ambiguous: set[str] = set()
        for row in rows:
            issue = self._file_integrity_issue(
                source_table="evidence",
                record_id=str(row["id"]),
                stored_path=row["path"],
                expected_sha256=row["sha256"],
            )
            if issue is not None:
                continue
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            kind = str(row["kind"])
            if kind == "SYNTHETIC_FIXTURE":
                source_type = "SYNTHETIC_FIXTURE"
                claim_scope = "EXACT_STATEMENT"
                raw_statements = payload.get(
                    "supported_statements",
                    [SYNTHETIC_FIXTURE_STATEMENT],
                )
                if not isinstance(raw_statements, list):
                    continue
                supported_statements = tuple(
                    item.strip()
                    for item in raw_statements
                    if isinstance(item, str) and item.strip()
                )
                if supported_statements != (SYNTHETIC_FIXTURE_STATEMENT,):
                    continue
            elif kind == "CEO_SUPPLIED_DOCUMENT":
                if (
                    payload.get("trust_basis") != "CEO_EXPLICIT_REGISTRATION"
                    or payload.get("source_type") != "USER_SUPPLIED_DOCUMENT"
                    or payload.get("claim_scope") != "GENERAL_DOCUMENT"
                ):
                    continue
                source_type = "USER_SUPPLIED_DOCUMENT"
                claim_scope = "GENERAL_DOCUMENT"
                supported_statements = ()
            else:
                continue
            grant = EvidenceGrant(
                kind=kind,
                source_type=source_type,
                claim_scope=claim_scope,
                supported_statements=supported_statements,
                trusted=True,
            )
            aliases = [str(row["id"])]
            if row["external_ref"]:
                aliases.append(str(row["external_ref"]))
            for alias in aliases:
                if alias in ambiguous:
                    continue
                prior = grants.get(alias)
                if prior is not None:
                    grants.pop(alias, None)
                    ambiguous.add(alias)
                else:
                    grants[alias] = grant
        return grants

    def _persist_contract(
        self,
        *,
        idea_id: str,
        contract: dict[str, Any],
        min_decision_level: str,
        gate_result: GateResult,
        idempotency_key: str,
        command: str,
        command_payload: dict[str, Any],
        resolved: bool = False,
    ) -> CompileOutcome:
        only_ceo_approval_missing = bool(gate_result.violations) and all(
            violation.code == "FP_FULL_CEO_APPROVAL_REQUIRED"
            for violation in gate_result.violations
        )
        gate_status = (
            "PASSED"
            if gate_result.passed
            else "PENDING_APPROVAL"
            if only_ceo_approval_missing
            else "FAILED"
        )
        idea_status = (
            "CONTRACT_COMPILED"
            if gate_status == "PASSED"
            else "APPROVAL_REQUIRED"
            if gate_status == "PENDING_APPROVAL"
            else "GATE_FAILED"
        )
        violation_payload = [
            {"code": v.code, "field": v.field, "message": v.message}
            for v in gate_result.violations
        ]

        def operation(connection: sqlite3.Connection) -> dict[str, str]:
            encoded_contract = self._json(contract)
            now = utc_now()
            existing_contract = connection.execute(
                """
                SELECT id, gate_status FROM contracts
                WHERE idea_id = ? AND payload_json = ? AND min_decision_level = ?
                ORDER BY created_at LIMIT 1
                """,
                (idea_id, encoded_contract, min_decision_level),
            ).fetchone()
            if existing_contract is not None:
                contract_id = str(existing_contract["id"])
                connection.execute(
                    "UPDATE contracts SET gate_status = ?, updated_at = ? WHERE id = ?",
                    (gate_status, now, contract_id),
                )
                connection.execute(
                    "UPDATE ideas SET status = ?, updated_at = ? WHERE id = ?",
                    (idea_status, now, idea_id),
                )
                self.store.append_event(
                    "FIRST_PRINCIPLES_GATE_EVALUATED",
                    aggregate_type="VentureContract",
                    aggregate_id=contract_id,
                    payload={
                        "passed": gate_result.passed,
                        "gate_status": gate_status,
                        "previous_gate_status": existing_contract["gate_status"],
                        "reevaluation": True,
                        "min_decision_level": min_decision_level,
                        "violations": violation_payload,
                    },
                    connection=connection,
                )
                if resolved:
                    self.store.append_event(
                        "COUNCIL_CONFLICT_RESOLVED",
                        aggregate_type="Idea",
                        aggregate_id=idea_id,
                        payload={
                            "contract_id": contract_id,
                            "resolution_method": "CEO_CONTRACT",
                        },
                        connection=connection,
                    )
                return {"contract_id": contract_id}
            contract_id = new_id("contract")
            self.store.insert_row(
                "contracts",
                {
                    "id": contract_id,
                    "idea_id": idea_id,
                    "decision_level": contract["decision_level"],
                    "min_decision_level": min_decision_level,
                    "gate_status": gate_status,
                    "payload_json": encoded_contract,
                    "created_at": now,
                    "updated_at": now,
                },
                connection=connection,
            )
            connection.execute(
                "UPDATE ideas SET status = ?, updated_at = ? WHERE id = ?",
                (idea_status, now, idea_id),
            )
            self.store.append_event(
                "FIRST_PRINCIPLES_GATE_EVALUATED",
                aggregate_type="VentureContract",
                aggregate_id=contract_id,
                payload={
                    "passed": gate_result.passed,
                    "gate_status": gate_status,
                    "reevaluation": False,
                    "min_decision_level": min_decision_level,
                    "violations": violation_payload,
                },
                connection=connection,
            )
            if resolved:
                self.store.append_event(
                    "COUNCIL_CONFLICT_RESOLVED",
                    aggregate_type="Idea",
                    aggregate_id=idea_id,
                    payload={
                        "contract_id": contract_id,
                        "resolution_method": "CEO_CONTRACT",
                    },
                    connection=connection,
                )
            return {"contract_id": contract_id}

        try:
            result = self.store.run_idempotent(
                idempotency_key, command, command_payload, operation
            )
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc
        return CompileOutcome(
            contract_id=str(result["contract_id"]),
            gate_result=gate_result,
        )

    def record_approval_and_scaffold(
        self,
        contract_id: str,
        *,
        approval_status: str,
        idempotency_key: str,
    ) -> Venture:
        contract_row = self._row("contracts", contract_id)
        contract = json.loads(contract_row["payload_json"])
        if contract_row["gate_status"] not in {"PASSED", "PENDING_APPROVAL"}:
            raise ValidationError("A Venture cannot be created before the Gate passes")
        normalized_approval = approval_status.strip().upper()
        if normalized_approval not in {"APPROVED", "NOT_REQUIRED"}:
            raise ValidationError("approval_status must be APPROVED or NOT_REQUIRED")
        if contract["decision_level"] == "FP_FULL" and normalized_approval != "APPROVED":
            raise ValidationError("FP_FULL requires explicit CEO approval")
        idea_id = contract_row["idea_id"]
        command_payload = {
            "contract_id": contract_id,
            "approval_status": normalized_approval,
        }
        staged_paths: list[Path] = []

        def operation(connection: sqlite3.Connection) -> dict[str, str]:
            venture_id = new_id("venture")
            metric_id = new_id("metric")
            experiment_id = new_id("experiment")
            work_order_id = new_id("work_order")
            decision_id = new_id("decision")
            approval_id = new_id("approval")
            now = utc_now()

            workspace = self.venture_workspace(venture_id)
            staging_workspace = contained_path(
                self.root,
                "var",
                "ventures",
                ".staging",
                venture_id,
            )
            staged_paths.append(staging_workspace)
            manifest_path = workspace / "context_manifest.json"
            staging_manifest_path = staging_workspace / "context_manifest.json"
            verifier_path = workspace / "work_orders" / work_order_id / "verifier.json"
            staging_verifier_path = (
                staging_workspace / "work_orders" / work_order_id / "verifier.json"
            )
            artifact_relative_path = "artifacts/verified_result.txt"
            expected_content = f"synthetic verified evidence for {idea_id}\n"
            approved_verifier_hash = write_exact_text_verifier(
                staging_verifier_path,
                artifact_relative_path=artifact_relative_path,
                expected_content=expected_content,
                contract_binding={
                    "metric": contract["metric"],
                    "experiment": contract["cheapest_valid_experiment"],
                    "pass_condition": contract["pass_condition"],
                    "fail_condition": contract["fail_condition"],
                },
            )

            assumption_claims: list[dict[str, Any]] = []
            for claim in contract.get("claims", []):
                if isinstance(claim, dict) and claim.get("type") == "ASSUMPTION":
                    assumption_claims.append(claim)
            for claim in contract.get("assumptions", []):
                if isinstance(claim, dict):
                    assumption_claims.append(claim)

            assumptions_by_external_ref: dict[str, dict[str, Any]] = {}
            for claim in assumption_claims:
                external_ref = str(claim.get("id", claim.get("claim_id", ""))).strip()
                if not external_ref:
                    raise ValidationError("Every persisted assumption requires an id")
                prior = assumptions_by_external_ref.get(external_ref)
                if prior is not None and prior != claim:
                    raise ValidationError(
                        f"Conflicting duplicate assumption id: {external_ref}"
                    )
                assumptions_by_external_ref[external_ref] = claim
            assumption_records = [
                (new_id("assumption"), external_ref, claim)
                for external_ref, claim in assumptions_by_external_ref.items()
            ]
            assumption_ids = [item[0] for item in assumption_records]
            idea_evidence_rows = connection.execute(
                """
                SELECT * FROM evidence
                WHERE idea_id = ?
                ORDER BY created_at, id
                """,
                (idea_id,),
            ).fetchall()
            evidence_grants = self._evidence_grants(
                idea_id,
                connection=connection,
            )
            invalid_trusted_evidence = [
                str(row["id"])
                for row in idea_evidence_rows
                if row["trusted"] == 1 and str(row["id"]) not in evidence_grants
            ]
            if invalid_trusted_evidence:
                raise ValidationError(
                    "Trusted Idea Evidence failed canonical integrity or policy "
                    "validation: " + ", ".join(invalid_trusted_evidence)
                )
            evidence_grants_fingerprint = self._evidence_grants_fingerprint(
                evidence_grants
            )
            venture_evidence_records: list[dict[str, Any]] = []
            for row in idea_evidence_rows:
                evidence_payload = json.loads(row["payload_json"])
                evidence_payload["origin_evidence_id"] = row["id"]
                venture_evidence_records.append(
                    {
                        "id": new_id("evidence"),
                        "idea_id": None,
                        "venture_id": venture_id,
                        "work_order_id": None,
                        "run_id": None,
                        "external_ref": row["external_ref"],
                        "kind": row["kind"],
                        "path": row["path"],
                        "sha256": row["sha256"],
                        "trusted": row["trusted"],
                        "payload_json": self._json(evidence_payload),
                        "created_at": now,
                    }
                )
            trusted_venture_evidence = [
                item for item in venture_evidence_records if item["trusted"] == 1
            ]

            manifest = {
                "schema_version": 1,
                "venture_id": venture_id,
                "idea_id": idea_id,
                "contract_id": contract_id,
                "decision_level": contract["decision_level"],
                "approval_status": normalized_approval,
                "assumption_ids": assumption_ids,
                "assumption_external_refs": {
                    internal_id: external_ref
                    for internal_id, external_ref, _ in assumption_records
                },
                "metric_ids": [metric_id],
                "experiment_ids": [experiment_id],
                "decision_ids": [decision_id],
                "work_order_ids": [work_order_id],
                "source_evidence_ids": [
                    item["id"] for item in trusted_venture_evidence
                ],
                "source_evidence_external_refs": [
                    str(item["external_ref"])
                    for item in trusted_venture_evidence
                    if item["external_ref"]
                ],
                "workspace_policy": "LOGICAL_NAMESPACE_ONLY",
                "isolation_mode": "LOGICAL_NAMESPACE_ONLY",
                "security_boundary": False,
                "created_at": now,
            }
            atomic_write_json(staging_manifest_path, manifest)

            self.store.insert_row(
                "approvals",
                {
                    "id": approval_id,
                    "contract_id": contract_id,
                    "decision_id": None,
                    "status": normalized_approval,
                    "actor": (
                        "CEO" if normalized_approval == "APPROVED" else "SYSTEM_POLICY"
                    ),
                    "payload_json": self._json(
                        {
                            "decision_level": contract["decision_level"],
                            "explicit_ceo_approval": normalized_approval == "APPROVED",
                        }
                    ),
                    "created_at": now,
                },
                connection=connection,
            )
            ceo_approved = (
                connection.execute(
                    """
                    SELECT COUNT(*) FROM approvals
                    WHERE contract_id = ? AND status = 'APPROVED' AND actor = 'CEO'
                    """,
                    (contract_id,),
                ).fetchone()[0]
                > 0
            )
            current_evidence_grants = self._evidence_grants(
                idea_id,
                connection=connection,
            )
            if (
                self._evidence_grants_fingerprint(current_evidence_grants)
                != evidence_grants_fingerprint
            ):
                raise ValidationError(
                    "Idea Evidence changed while creating the Venture context"
                )
            gate_result = self.gate.validate(
                contract,
                min_decision_level=contract_row["min_decision_level"],
                trusted_evidence_refs=frozenset(current_evidence_grants),
                evidence_grants=current_evidence_grants,
                ceo_approved=ceo_approved,
            )
            if not gate_result.passed:
                codes = ", ".join(item.code for item in gate_result.violations)
                raise ValidationError(
                    f"A Venture cannot be created before the Gate passes: {codes}"
                )
            connection.execute(
                "UPDATE contracts SET gate_status = 'PASSED', updated_at = ? WHERE id = ?",
                (now, contract_id),
            )

            self.store.insert_row(
                "ventures",
                {
                    "id": venture_id,
                    "idea_id": idea_id,
                    "contract_id": contract_id,
                    "status": "ACTIVE",
                    "workspace_path": self._relative(workspace),
                    "context_manifest_path": self._relative(manifest_path),
                    "metadata_json": self._json(
                        {
                            "context_isolation": "LOGICAL_NAMESPACE_ONLY",
                            "security_boundary": False,
                        }
                    ),
                    "created_at": now,
                    "updated_at": now,
                },
                connection=connection,
            )
            for evidence_record in venture_evidence_records:
                self.store.insert_row(
                    "evidence",
                    evidence_record,
                    connection=connection,
                )
            for assumption_id, external_ref, claim in assumption_records:
                self.store.insert_row(
                    "assumptions",
                    {
                        "id": assumption_id,
                        "venture_id": venture_id,
                        "contract_id": contract_id,
                        "external_ref": external_ref,
                        "statement": str(claim["statement"]),
                        "status": "UNTESTED",
                        "classification": "ASSUMPTION",
                        "payload_json": self._json(claim),
                        "created_at": now,
                        "updated_at": now,
                    },
                    connection=connection,
                )

            metric = contract["metric"]
            self.store.insert_row(
                "metrics",
                {
                    "id": metric_id,
                    "venture_id": venture_id,
                    "name": metric["name"],
                    "formula": metric["formula"],
                    "unit": metric["unit"],
                    "time_window": metric["time_window"],
                    "data_source": metric["data_source"],
                    "version": 1,
                    "payload_json": self._json(metric),
                    "created_at": now,
                    "updated_at": now,
                },
                connection=connection,
            )
            self.store.insert_row(
                "experiments",
                {
                    "id": experiment_id,
                    "venture_id": venture_id,
                    "assumption_id": assumption_ids[0] if assumption_ids else None,
                    "metric_id": metric_id,
                    "status": "PLANNED",
                    "payload_json": self._json(contract["cheapest_valid_experiment"]),
                    "created_at": now,
                    "updated_at": now,
                },
                connection=connection,
            )
            work_specification = {
                "objective": "Create one deterministic synthetic artifact.",
                "artifact_relative_path": artifact_relative_path,
                "expected_content": expected_content,
                "acceptance": {
                    "pass_condition": contract["pass_condition"],
                    "fail_condition": contract["fail_condition"],
                },
                "execution_mode": "manual_or_port",
                "verification_scope": "SYNTHETIC_ONLY",
                "verifier_semantics": "EXACT_TEXT_FIXTURE_ONLY",
            }
            self.store.insert_row(
                "work_orders",
                {
                    "id": work_order_id,
                    "venture_id": venture_id,
                    "experiment_id": experiment_id,
                    "title": "Create and verify one synthetic artifact",
                    "status": "READY",
                    "specification_json": self._json(work_specification),
                    "verifier_path": self._relative(verifier_path),
                    "verifier_sha256": approved_verifier_hash,
                    "created_at": now,
                    "updated_at": now,
                },
                connection=connection,
            )
            self.store.insert_row(
                "decisions",
                {
                    "id": decision_id,
                    "venture_id": venture_id,
                    "work_order_id": work_order_id,
                    "status": "RECORDED",
                    "payload_json": self._json(
                        {
                            "classification": "DECISION",
                            "selection": "Run the cheapest valid synthetic experiment.",
                            "evidence_provenance": contract["decision_basis"],
                            "reversibility": "REVERSIBLE_LOCAL",
                        }
                    ),
                    "created_at": now,
                    "updated_at": now,
                },
                connection=connection,
            )
            connection.execute(
                "UPDATE ideas SET status = 'VENTURE_CREATED', updated_at = ? WHERE id = ?",
                (now, idea_id),
            )
            self.store.append_event(
                "APPROVAL_STATUS_RECORDED",
                aggregate_type="VentureContract",
                aggregate_id=contract_id,
                venture_id=venture_id,
                payload={"status": normalized_approval},
                connection=connection,
            )
            self.store.append_event(
                "VENTURE_SCAFFOLDED",
                aggregate_type="Venture",
                aggregate_id=venture_id,
                venture_id=venture_id,
                payload={
                    "workspace_path": self._relative(workspace),
                    "context_manifest_path": self._relative(manifest_path),
                    "work_order_id": work_order_id,
                },
                connection=connection,
            )
            return {
                "venture_id": venture_id,
                "staging_workspace": self._relative(staging_workspace),
            }

        try:
            result = self.store.run_idempotent(
                idempotency_key,
                "record_approval_and_scaffold",
                command_payload,
                operation,
            )
        except IdempotencyConflict as exc:
            for path in staged_paths:
                if path.exists():
                    shutil.rmtree(path)
            raise ConflictError(str(exc)) from exc
        except BaseException:
            for path in staged_paths:
                if path.exists():
                    shutil.rmtree(path)
            raise
        venture_id = str(result["venture_id"])
        workspace = self.venture_workspace(venture_id)
        staging_relative = result.get("staging_workspace")
        staging_workspace = (
            self._absolute(str(staging_relative))
            if staging_relative
            else contained_path(
                self.root, "var", "ventures", ".staging", venture_id
            )
        )
        if not workspace.exists():
            try:
                workspace.parent.mkdir(parents=True, exist_ok=True)
                staging_workspace.replace(workspace)
            except OSError as exc:
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE ventures SET status = 'SCAFFOLD_FAILED', updated_at = ? "
                        "WHERE id = ?",
                        (utc_now(), venture_id),
                    )
                    self.store.append_event(
                        "VENTURE_SCAFFOLD_PROMOTION_FAILED",
                        aggregate_type="Venture",
                        aggregate_id=venture_id,
                        venture_id=venture_id,
                        payload={"error": type(exc).__name__},
                        connection=connection,
                    )
                raise ValidationError(
                    "Venture workspace could not be promoted from staging"
                ) from exc
        elif staging_workspace.exists():
            shutil.rmtree(staging_workspace)
        staging_root = staging_workspace.parent
        if staging_root.is_dir() and not any(staging_root.iterdir()):
            staging_root.rmdir()
        current_venture = self.venture(venture_id)
        if current_venture.status == "SCAFFOLD_FAILED":
            with self.store.transaction() as connection:
                recovered_at = utc_now()
                connection.execute(
                    "UPDATE ventures SET status = 'ACTIVE', updated_at = ? WHERE id = ?",
                    (recovered_at, venture_id),
                )
                self.store.append_event(
                    "VENTURE_SCAFFOLD_PROMOTION_RECOVERED",
                    aggregate_type="Venture",
                    aggregate_id=venture_id,
                    venture_id=venture_id,
                    payload={"workspace_path": self._relative(workspace)},
                    connection=connection,
                )
        return self.venture(venture_id)

    def venture_workspace(self, venture_id: str) -> Path:
        if not _VENTURE_ID.fullmatch(venture_id):
            raise ValueError(f"invalid Venture id: {venture_id}")
        return contained_path(self.root, "var", "ventures", venture_id)

    def venture(self, venture_id: str) -> Venture:
        row = self._row("ventures", venture_id)
        return Venture(
            id=row["id"],
            contract_id=row["contract_id"],
            idea_id=row["idea_id"],
            workspace_path=self._absolute(row["workspace_path"]),
            context_manifest_path=self._absolute(row["context_manifest_path"]),
            status=row["status"],
        )

    def first_work_order(self, venture_id: str) -> WorkOrder:
        row = self.store.query_one(
            "SELECT * FROM work_orders WHERE venture_id = ? ORDER BY created_at LIMIT 1",
            (venture_id,),
        )
        if row is None:
            raise NotFoundError(f"No WorkOrder exists for Venture: {venture_id}")
        return self._work_order_from_row(row)

    def work_order(self, work_order_id: str) -> WorkOrder:
        return self._work_order_from_row(self._row("work_orders", work_order_id))

    def _work_order_from_row(self, row: sqlite3.Row) -> WorkOrder:
        spec = json.loads(row["specification_json"])
        return WorkOrder(
            id=row["id"],
            venture_id=row["venture_id"],
            title=row["title"],
            status=row["status"],
            artifact_relative_path=spec["artifact_relative_path"],
            expected_content=spec["expected_content"],
            verifier_path=self._absolute(row["verifier_path"]),
            verifier_hash=row["verifier_sha256"],
        )

    def execute_work_order(
        self,
        work_order_id: str,
        *,
        executor: ExecutorPort,
        idempotency_key: str,
    ) -> Run:
        """Execute an ordinary WorkOrder.

        Repair execution is deliberately unavailable through this public
        entrypoint because it requires a validated required-change manifest.
        """

        return self._execute_work_order(
            work_order_id,
            executor=executor,
            idempotency_key=idempotency_key,
        )

    def _execute_work_order(
        self,
        work_order_id: str,
        *,
        executor: ExecutorPort,
        idempotency_key: str,
        _repair_mode: bool = False,
        _repair_manifest: dict[str, Any] | None = None,
    ) -> Run:
        if _repair_mode:
            if _repair_manifest is None:
                raise ValidationError("Repair execution requires a repair manifest")
            # The execution boundary validates again even when its caller is an
            # internal helper.  A leading underscore is not an authorization
            # boundary in Python and must not weaken repair invariants.
            _repair_manifest = self._validate_repair_manifest(
                work_order_id,
                _repair_manifest,
            )
        if self.is_stopped():
            raise CompanyStoppedError("Company execution is stopped; run company resume")
        work_order = self.work_order(work_order_id)
        permitted_statuses = (
            {"REPAIR_REQUIRED"}
            if _repair_mode
            else {"READY", "VERIFICATION_FAILED"}
        )
        if work_order.status not in permitted_statuses:
            # Exact idempotent replays still return the original result.
            existing = self.store.get_row("idempotency", idempotency_key)
            if existing is None or existing["status"] != "COMPLETED":
                raise ValidationError(
                    f"WorkOrder cannot execute from status {work_order.status}"
                )
        command_payload = {
            "work_order_id": work_order_id,
            "executor": executor.name,
            "approved_verifier_hash": work_order.verifier_hash,
            "repair_mode": _repair_mode,
            "repair_manifest_hash": (
                payload_hash(_repair_manifest) if _repair_manifest is not None else None
            ),
        }

        def operation(connection: sqlite3.Connection) -> dict[str, str]:
            current_row = connection.execute(
                "SELECT * FROM work_orders WHERE id = ?", (work_order_id,)
            ).fetchone()
            if current_row is None:
                raise NotFoundError(f"WorkOrder not found: {work_order_id}")
            current_work = self._work_order_from_row(current_row)
            if current_work.status not in permitted_statuses:
                raise ValidationError(
                    f"WorkOrder cannot execute from status {current_work.status}"
                )
            venture = self.venture(current_work.venture_id)
            attempt = int(
                connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE work_order_id = ?",
                    (work_order_id,),
                ).fetchone()[0]
            ) + 1
            run_id = new_id("run")
            now = utc_now()
            try:
                observed_before = verifier_hash(current_work.verifier_path)
            except OSError:
                observed_before = "MISSING"
            if observed_before != current_work.verifier_hash:
                return self._record_tampered_run(
                    connection,
                    current_work,
                    venture,
                    run_id=run_id,
                    attempt=attempt,
                    executor_name=executor.name,
                    observed_hash=observed_before,
                    occurred_at=now,
                    artifact_path=None,
                )

            output = executor.execute(current_work, venture.workspace_path)
            artifact = output.artifact_path.resolve()
            workspace = venture.workspace_path.resolve()
            if workspace not in artifact.parents:
                raise ValidationError("Executor returned an artifact outside its Venture")
            expected_artifact = contained_path(
                venture.workspace_path, current_work.artifact_relative_path
            )
            if artifact != expected_artifact:
                return self._record_invalid_artifact_run(
                    connection,
                    current_work,
                    venture,
                    run_id=run_id,
                    attempt=attempt,
                    executor_name=executor.name,
                    artifact_path=artifact,
                    occurred_at=now,
                )

            try:
                observed_after = verifier_hash(current_work.verifier_path)
            except OSError:
                observed_after = "MISSING"
            if observed_after != current_work.verifier_hash:
                return self._record_tampered_run(
                    connection,
                    current_work,
                    venture,
                    run_id=run_id,
                    attempt=attempt,
                    executor_name=executor.name,
                    observed_hash=observed_after,
                    occurred_at=now,
                    artifact_path=artifact,
                )

            verification = run_verifier(
                current_work.verifier_path, venture.workspace_path
            )
            finished = utc_now()
            verifier_output_path = contained_path(
                venture.workspace_path, "runs", run_id, "verifier_output.json"
            )
            verifier_payload = result_as_dict(verification)
            verifier_payload.update(
                {
                    "run_id": run_id,
                    "work_order_id": work_order_id,
                    "approved_verifier_hash": current_work.verifier_hash,
                    "observed_verifier_hash": observed_after,
                }
            )
            atomic_write_json(verifier_output_path, verifier_payload)
            self.store.insert_row(
                "runs",
                {
                    "id": run_id,
                    "work_order_id": work_order_id,
                    "status": verification.status,
                    "executor": executor.name,
                    "verifier_sha256": current_work.verifier_hash,
                    "payload_json": self._json(
                        {
                            "attempt": attempt,
                            "verifier_output_path": self._relative(verifier_output_path),
                            "verification": verifier_payload,
                            "repair_manifest": _repair_manifest,
                        }
                    ),
                    "started_at": now,
                    "finished_at": finished,
                    "created_at": now,
                },
                connection=connection,
            )
            artifact_sha = sha256_file(artifact)
            artifact_evidence_path = contained_path(
                venture.workspace_path,
                "runs",
                run_id,
                "artifact_snapshot",
                artifact.name,
            )
            artifact_evidence_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact, artifact_evidence_path)
            artifact_evidence_sha = sha256_file(artifact_evidence_path)
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, venture_id, work_order_id, run_id, path, sha256,
                    media_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(venture_id, path) DO UPDATE SET
                    work_order_id = excluded.work_order_id,
                    run_id = excluded.run_id,
                    sha256 = excluded.sha256,
                    media_type = excluded.media_type,
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at
                """,
                (
                    new_id("artifact"),
                    venture.id,
                    work_order_id,
                    run_id,
                    self._relative(artifact),
                    artifact_sha,
                    "text/plain",
                    self._json(
                        {
                            "synthetic": True,
                            "verification_scope": "SYNTHETIC_ONLY",
                        }
                    ),
                    finished,
                ),
            )
            run_evidence_ids: list[str] = []
            for kind, path, digest in (
                (
                    "RUN_ARTIFACT",
                    artifact_evidence_path,
                    artifact_evidence_sha,
                ),
                (
                    "VERIFIER_OUTPUT",
                    verifier_output_path,
                    sha256_file(verifier_output_path),
                ),
            ):
                evidence_id = new_id("evidence")
                self.store.insert_row(
                    "evidence",
                    {
                        "id": evidence_id,
                        "idea_id": None,
                        "venture_id": venture.id,
                        "work_order_id": work_order_id,
                        "run_id": run_id,
                        "external_ref": None,
                        "kind": kind,
                        "path": self._relative(path),
                        "sha256": digest,
                        "trusted": 1,
                        "payload_json": self._json(
                            {
                                "trusted": True,
                                "verification_status": verification.status,
                                "verification_scope": "SYNTHETIC_ONLY",
                                "verifier_semantics": "EXACT_TEXT_FIXTURE_ONLY",
                            }
                        ),
                        "created_at": finished,
                    },
                    connection=connection,
                )
                run_evidence_ids.append(evidence_id)
            if _repair_mode:
                next_work_status = (
                    "AWAITING_REREVIEW"
                    if verification.status == "PASS"
                    else "REPAIR_REQUIRED"
                )
            else:
                next_work_status = (
                    "VERIFIED"
                    if verification.status == "PASS"
                    else "VERIFICATION_FAILED"
                )
            connection.execute(
                "UPDATE work_orders SET status = ?, updated_at = ? WHERE id = ?",
                (next_work_status, finished, work_order_id),
            )
            if _repair_mode and verification.status == "PASS":
                if _repair_manifest is None:
                    raise ValidationError("Repair PASS requires a repair manifest")
                repair_snapshot = self._source_snapshot()
                if (
                    repair_snapshot.source_commit
                    != _repair_manifest["source_commit"]
                    or repair_snapshot.source_tree_sha256
                    != _repair_manifest["source_tree_sha256"]
                ):
                    raise ValidationError(
                        "Source changed while executing the repair WorkOrder"
                    )
                for change in _repair_manifest["changes"]:
                    canonical_evidence_ids = self._canonical_repair_evidence_ids(
                        work_order_id,
                        change["evidence_ids"],
                        connection=connection,
                        review_id=_repair_manifest["review_id"],
                        required_change_id=change["id"],
                        source_commit=_repair_manifest["source_commit"],
                        source_tree_sha256=_repair_manifest[
                            "source_tree_sha256"
                        ],
                        require_test_result=True,
                    )
                    self.store.insert_row(
                        "review_change_resolutions",
                        {
                            "id": new_id("change_resolution"),
                            "review_id": _repair_manifest["review_id"],
                            "required_change_id": change["id"],
                            "repair_run_id": run_id,
                            "source_commit": change["commit"],
                            "source_tree_sha256": _repair_manifest[
                                "source_tree_sha256"
                            ],
                            "evidence_policy_version": 1,
                            "evidence_json": self._json(
                                sorted(
                                    set(canonical_evidence_ids + run_evidence_ids)
                                )
                            ),
                            "created_at": finished,
                        },
                        connection=connection,
                    )
                connection.execute(
                    """
                    UPDATE review_required_changes
                    SET status = 'SUBMITTED', updated_at = ?
                    WHERE review_id = ? AND change_id IN (
                        SELECT required_change_id
                        FROM review_change_resolutions
                        WHERE review_id = ? AND repair_run_id = ?
                    )
                    """,
                    (
                        finished,
                        _repair_manifest["review_id"],
                        _repair_manifest["review_id"],
                        run_id,
                    ),
                )
                submitted_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM review_required_changes
                        WHERE review_id = ? AND status = 'SUBMITTED'
                        """,
                        (_repair_manifest["review_id"],),
                    ).fetchone()[0]
                )
                if submitted_count != len(_repair_manifest["changes"]):
                    raise ValidationError(
                        "Repair resolutions do not match required_change records"
                    )
            self.store.append_event(
                "WORK_ORDER_VERIFIED",
                aggregate_type="WorkOrder",
                aggregate_id=work_order_id,
                venture_id=venture.id,
                payload={
                    "run_id": run_id,
                    "status": verification.status,
                    "attempt": attempt,
                    "artifact_sha256": artifact_sha,
                    "verification_scope": "SYNTHETIC_ONLY",
                },
                connection=connection,
            )
            if _repair_mode:
                self.store.append_event(
                    "REPAIR_REVERIFIED",
                    aggregate_type="WorkOrder",
                    aggregate_id=work_order_id,
                    venture_id=venture.id,
                    payload={"run_id": run_id, "status": verification.status},
                    connection=connection,
                )
            return {"run_id": run_id}

        try:
            result = self.store.run_idempotent(
                idempotency_key,
                "repair_work_order" if _repair_mode else "execute_work_order",
                command_payload,
                operation,
            )
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc
        return self.run(str(result["run_id"]))

    def _record_tampered_run(
        self,
        connection: sqlite3.Connection,
        work_order: WorkOrder,
        venture: Venture,
        *,
        run_id: str,
        attempt: int,
        executor_name: str,
        observed_hash: str,
        occurred_at: str,
        artifact_path: Path | None,
    ) -> dict[str, str]:
        self.store.insert_row(
            "runs",
            {
                "id": run_id,
                "work_order_id": work_order.id,
                "status": "INVALIDATED",
                "executor": executor_name,
                "verifier_sha256": work_order.verifier_hash,
                "payload_json": self._json(
                    {
                        "attempt": attempt,
                        "reason": "VERIFIER_TAMPER_DETECTED",
                        "approved_hash": work_order.verifier_hash,
                        "observed_hash": observed_hash,
                    }
                ),
                "started_at": occurred_at,
                "finished_at": occurred_at,
                "created_at": occurred_at,
            },
            connection=connection,
        )
        evidence_path = artifact_path or work_order.verifier_path
        evidence_digest = (
            sha256_file(evidence_path) if evidence_path.is_file() else None
        )
        self.store.insert_row(
            "evidence",
            {
                "id": new_id("evidence"),
                "idea_id": None,
                "venture_id": venture.id,
                "work_order_id": work_order.id,
                "run_id": run_id,
                "external_ref": None,
                "kind": "UNTRUSTED_ARTIFACT" if artifact_path else "TAMPERED_VERIFIER",
                "path": self._relative(evidence_path),
                "sha256": evidence_digest,
                "trusted": 0,
                "payload_json": self._json(
                    {"trusted": False, "reason": "VERIFIER_TAMPER_DETECTED"}
                ),
                "created_at": occurred_at,
            },
            connection=connection,
        )
        connection.execute(
            "UPDATE work_orders SET status = 'INVALIDATED', updated_at = ? WHERE id = ?",
            (occurred_at, work_order.id),
        )
        self.store.append_event(
            "VERIFIER_TAMPER_DETECTED",
            aggregate_type="WorkOrder",
            aggregate_id=work_order.id,
            venture_id=venture.id,
            payload={
                "run_id": run_id,
                "approved_hash": work_order.verifier_hash,
                "observed_hash": observed_hash,
            },
            connection=connection,
        )
        return {"run_id": run_id}

    def _record_invalid_artifact_run(
        self,
        connection: sqlite3.Connection,
        work_order: WorkOrder,
        venture: Venture,
        *,
        run_id: str,
        attempt: int,
        executor_name: str,
        artifact_path: Path,
        occurred_at: str,
    ) -> dict[str, str]:
        self.store.insert_row(
            "runs",
            {
                "id": run_id,
                "work_order_id": work_order.id,
                "status": "INVALIDATED",
                "executor": executor_name,
                "verifier_sha256": work_order.verifier_hash,
                "payload_json": self._json(
                    {
                        "attempt": attempt,
                        "reason": "EXECUTOR_ARTIFACT_PATH_MISMATCH",
                        "expected_path": work_order.artifact_relative_path,
                        "returned_path": self._relative(artifact_path),
                    }
                ),
                "started_at": occurred_at,
                "finished_at": occurred_at,
                "created_at": occurred_at,
            },
            connection=connection,
        )
        self.store.insert_row(
            "evidence",
            {
                "id": new_id("evidence"),
                "idea_id": None,
                "venture_id": venture.id,
                "work_order_id": work_order.id,
                "run_id": run_id,
                "external_ref": None,
                "kind": "UNTRUSTED_ARTIFACT",
                "path": self._relative(artifact_path),
                "sha256": sha256_file(artifact_path) if artifact_path.is_file() else None,
                "trusted": 0,
                "payload_json": self._json(
                    {
                        "trusted": False,
                        "reason": "EXECUTOR_ARTIFACT_PATH_MISMATCH",
                    }
                ),
                "created_at": occurred_at,
            },
            connection=connection,
        )
        connection.execute(
            "UPDATE work_orders SET status = 'INVALIDATED', updated_at = ? WHERE id = ?",
            (occurred_at, work_order.id),
        )
        self.store.append_event(
            "EXECUTOR_ARTIFACT_PATH_MISMATCH",
            aggregate_type="WorkOrder",
            aggregate_id=work_order.id,
            venture_id=venture.id,
            payload={
                "run_id": run_id,
                "expected_path": work_order.artifact_relative_path,
                "returned_path": self._relative(artifact_path),
            },
            connection=connection,
        )
        return {"run_id": run_id}

    def verify_existing_artifact(
        self, work_order_id: str, *, idempotency_key: str
    ) -> Run:
        return self.execute_work_order(
            work_order_id,
            executor=ExistingArtifactExecutor(),
            idempotency_key=idempotency_key,
        )

    def run(self, run_id: str) -> Run:
        row = self._row("runs", run_id)
        payload = json.loads(row["payload_json"])
        return Run(
            id=row["id"],
            work_order_id=row["work_order_id"],
            status=row["status"],
            attempt=int(payload.get("attempt", 1)),
            verifier_hash=row["verifier_sha256"] or "",
        )

    def runs_for_work_order(self, work_order_id: str) -> tuple[Run, ...]:
        return tuple(
            self.run(row["id"])
            for row in self.store.query_all(
                "SELECT id FROM runs WHERE work_order_id = ? ORDER BY created_at",
                (work_order_id,),
            )
        )

    def evidence_for_run(self, run_id: str) -> tuple[Evidence, ...]:
        rows = self.store.query_all(
            "SELECT * FROM evidence WHERE run_id = ? ORDER BY created_at, id",
            (run_id,),
        )
        items: list[Evidence] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            items.append(
                Evidence(
                    id=row["id"],
                    venture_id=row["venture_id"],
                    work_order_id=row["work_order_id"],
                    run_id=row["run_id"],
                    kind=row["kind"],
                    path=self._absolute(row["path"]),
                    sha256=row["sha256"],
                    trusted=bool(payload.get("trusted", False)),
                )
            )
        return tuple(items)

    def _file_integrity_issue(
        self,
        *,
        source_table: str,
        record_id: str,
        stored_path: Any,
        expected_sha256: Any,
    ) -> dict[str, Any] | None:
        issue = {
            "source_table": source_table,
            "record_id": record_id,
            "path": stored_path,
            "expected_sha256": expected_sha256,
        }
        if not isinstance(stored_path, str) or not stored_path:
            return {**issue, "observed_sha256": None, "reason": "PATH_MISSING"}
        if not isinstance(expected_sha256, str) or not expected_sha256:
            return {
                **issue,
                "observed_sha256": None,
                "reason": "EXPECTED_SHA256_MISSING",
            }
        try:
            path = self._absolute(stored_path)
            observed = sha256_file(path)
        except (OSError, TypeError, ValueError):
            return {**issue, "observed_sha256": None, "reason": "FILE_UNREADABLE"}
        if observed != expected_sha256:
            return {
                **issue,
                "observed_sha256": observed,
                "reason": "SHA256_MISMATCH",
            }
        return None

    def _review_materials(
        self,
        work_order_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[sqlite3.Row | None, list[sqlite3.Row], list[sqlite3.Row]]:
        conn = connection or self.store.connection
        latest_run = conn.execute(
            """
            SELECT * FROM runs
            WHERE work_order_id = ? AND status = 'PASS'
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (work_order_id,),
        ).fetchone()
        if latest_run is None:
            return None, [], []
        evidence_rows = list(
            conn.execute(
                """
                SELECT id, kind, path, sha256 FROM evidence
                WHERE run_id = ? ORDER BY id
                """,
                (latest_run["id"],),
            ).fetchall()
        )
        artifact_rows = list(
            conn.execute(
                """
                SELECT id, path, sha256, media_type FROM artifacts
                WHERE run_id = ? ORDER BY id
                """,
                (latest_run["id"],),
            ).fetchall()
        )
        return latest_run, evidence_rows, artifact_rows

    def _review_integrity_issues(
        self,
        latest_run: sqlite3.Row,
        evidence_rows: Iterable[sqlite3.Row],
        artifact_rows: Iterable[sqlite3.Row],
    ) -> list[dict[str, Any]]:
        evidence_items = list(evidence_rows)
        artifact_items = list(artifact_rows)
        issues: list[dict[str, Any]] = []
        if not evidence_items:
            issues.append(
                {
                    "source_table": "evidence",
                    "record_id": latest_run["id"],
                    "path": None,
                    "expected_sha256": None,
                    "observed_sha256": None,
                    "reason": "METADATA_MISSING",
                }
            )
        if not artifact_items:
            issues.append(
                {
                    "source_table": "artifacts",
                    "record_id": latest_run["id"],
                    "path": None,
                    "expected_sha256": None,
                    "observed_sha256": None,
                    "reason": "METADATA_MISSING",
                }
            )
        for source_table, rows in (
            ("evidence", evidence_items),
            ("artifacts", artifact_items),
        ):
            for row in rows:
                issue = self._file_integrity_issue(
                    source_table=source_table,
                    record_id=str(row["id"]),
                    stored_path=row["path"],
                    expected_sha256=row["sha256"],
                )
                if issue is not None:
                    issues.append(issue)
        return issues

    def _bound_review_material_issues(
        self,
        review_row: sqlite3.Row,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Revalidate the exact PASS Run bound into a ReviewRequest."""

        latest_run, evidence_rows, artifact_rows = self._review_materials(
            str(review_row["work_order_id"]),
            connection=connection,
        )
        if latest_run is None or latest_run["id"] != review_row["run_id"]:
            return [
                {
                    "source_table": "runs",
                    "record_id": str(review_row["run_id"]),
                    "path": None,
                    "expected_sha256": None,
                    "observed_sha256": None,
                    "reason": "BOUND_RUN_MISSING_OR_NOT_LATEST",
                }
            ]
        return self._review_integrity_issues(
            latest_run,
            evidence_rows,
            artifact_rows,
        )

    def _review_resolution_integrity_issues(
        self,
        review_row: sqlite3.Row,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Rehash every canonical Evidence record cited by repair lineage."""

        payload = json.loads(review_row["payload_json"])
        request = payload.get("request")
        if not isinstance(request, dict):
            return []
        resolutions = request.get("change_resolutions", [])
        if not isinstance(resolutions, list):
            return [
                {
                    "source_table": "review_change_resolutions",
                    "record_id": str(review_row["id"]),
                    "path": None,
                    "expected_sha256": None,
                    "observed_sha256": None,
                    "reason": "RESOLUTION_STRUCTURE_INVALID",
                }
            ]
        for resolution in resolutions:
            if not isinstance(resolution, dict) or not isinstance(
                resolution.get("evidence_ids"), list
            ):
                return [
                    {
                        "source_table": "review_change_resolutions",
                        "record_id": str(review_row["id"]),
                        "path": None,
                        "expected_sha256": None,
                        "observed_sha256": None,
                        "reason": "RESOLUTION_STRUCTURE_INVALID",
                    }
                ]
            policy = resolution.get("evidence_policy", {})
            require_test_result = (
                isinstance(policy, dict)
                and policy.get("required_kind") == "TEST_RESULT"
            )
            try:
                self._canonical_repair_evidence_ids(
                    str(review_row["work_order_id"]),
                    [str(item) for item in resolution["evidence_ids"]],
                    connection=connection,
                    review_id=(
                        str(resolution["origin_review_id"])
                        if resolution.get("origin_review_id") is not None
                        else None
                    ),
                    required_change_id=(
                        str(resolution["required_change_id"])
                        if resolution.get("required_change_id") is not None
                        else None
                    ),
                    source_commit=(
                        str(resolution["source_commit"])
                        if resolution.get("source_commit") is not None
                        else None
                    ),
                    source_tree_sha256=(
                        str(resolution["source_tree_sha256"])
                        if resolution.get("source_tree_sha256") is not None
                        else None
                    ),
                    require_test_result=require_test_result,
                )
            except ValidationError as exc:
                return [
                    {
                        "source_table": "review_change_resolutions",
                        "record_id": str(review_row["id"]),
                        "path": None,
                        "expected_sha256": None,
                        "observed_sha256": None,
                        "reason": "RESOLUTION_EVIDENCE_INVALID",
                        "message": str(exc),
                    }
                ]
        return []

    def _review_attachment_integrity_issues(
        self,
        request: dict[str, Any],
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        for collection_name in ("artifacts", "evidence"):
            collection = request.get(collection_name, [])
            if not isinstance(collection, list):
                continue
            for item in collection:
                if not isinstance(item, dict) or item.get("inline") is not False:
                    continue
                issue = self._file_integrity_issue(
                    source_table="review_attachment",
                    record_id=str(item.get("id", "unknown")),
                    stored_path=item.get("attachment_path"),
                    expected_sha256=item.get("attachment_sha256"),
                )
                if issue is not None:
                    issues.append(issue)
        return issues

    @staticmethod
    def _expected_review_markdown_sha256(
        stored_payload: dict[str, Any],
    ) -> str | None:
        """Derive the Markdown digest from canonical DB-owned request data.

        Derivation provides a safe compatibility path for reviews created
        before ``request_markdown_sha256`` was persisted.  The current file is
        never used as the source of the expected digest.
        """

        request = stored_payload.get("request")
        if not isinstance(request, dict):
            return None
        stored = stored_payload.get("request_markdown_sha256")
        if isinstance(stored, str) and stored:
            return stored
        rendered = review_request_markdown(request).encode("utf-8")
        return sha256(rendered).hexdigest()

    def _review_request_integrity_issues(
        self,
        review_row: sqlite3.Row,
    ) -> list[dict[str, Any]]:
        """Validate both ReviewRequest representations and attachments."""

        review_id = str(review_row["id"])
        try:
            stored_payload = json.loads(review_row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return [
                {
                    "source_table": "review_request",
                    "record_id": review_id,
                    "path": None,
                    "reason": "CANONICAL_PAYLOAD_INVALID",
                }
            ]
        if not isinstance(stored_payload, dict):
            return [
                {
                    "source_table": "review_request",
                    "record_id": review_id,
                    "path": None,
                    "reason": "CANONICAL_PAYLOAD_INVALID",
                }
            ]

        issues: list[dict[str, Any]] = []
        json_stored_path = review_row["request_json_path"]
        expected_json_sha = stored_payload.get("request_json_sha256")
        json_issue = self._file_integrity_issue(
            source_table="review_request",
            record_id=review_id,
            stored_path=json_stored_path,
            expected_sha256=expected_json_sha,
        )
        if json_issue is not None:
            issues.append(json_issue)

        actual_request: Any = None
        try:
            actual_request = read_json(self._absolute(json_stored_path))
        except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            issues.append(
                {
                    "source_table": "review_request",
                    "record_id": review_id,
                    "path": json_stored_path,
                    "reason": "INVALID_JSON",
                }
            )
        if isinstance(actual_request, dict):
            embedded_hash = actual_request.get("review_request_hash")
            request_core = dict(actual_request)
            request_core.pop("review_request_hash", None)
            observed_payload_hash = payload_hash(request_core)
            expected_payload_hash = stored_payload.get("request_hash")
            if (
                embedded_hash != expected_payload_hash
                or observed_payload_hash != expected_payload_hash
                or actual_request != stored_payload.get("request")
            ):
                issues.append(
                    {
                        "source_table": "review_request",
                        "record_id": review_id,
                        "path": json_stored_path,
                        "expected_payload_hash": expected_payload_hash,
                        "embedded_payload_hash": embedded_hash,
                        "observed_payload_hash": observed_payload_hash,
                        "reason": "PAYLOAD_HASH_MISMATCH",
                    }
                )
            issues.extend(
                self._review_attachment_integrity_issues(actual_request)
            )
        elif actual_request is not None:
            issues.append(
                {
                    "source_table": "review_request",
                    "record_id": review_id,
                    "path": json_stored_path,
                    "reason": "REQUEST_NOT_OBJECT",
                }
            )

        canonical_request = stored_payload.get("request")
        candidate_markdown_hashes: set[str] = set()
        if isinstance(canonical_request, dict):
            candidate_markdown_hashes.add(
                sha256(
                    review_request_markdown(canonical_request).encode("utf-8")
                ).hexdigest()
            )
        # Compatibility for ReviewRequests emitted before the renderer became
        # key-sorted.  The legacy candidate is derived only from the separately
        # hash-bound JSON file, never from the Markdown file under inspection.
        if (
            isinstance(actual_request, dict)
            and actual_request == canonical_request
        ):
            candidate_markdown_hashes.add(
                sha256(
                    markdown_document(
                        REVIEW_REQUEST_TITLE,
                        actual_request,
                    ).encode("utf-8")
                ).hexdigest()
            )
        stored_markdown_sha = stored_payload.get("request_markdown_sha256")
        if stored_markdown_sha is not None and (
            not isinstance(stored_markdown_sha, str)
            or stored_markdown_sha not in candidate_markdown_hashes
        ):
            issues.append(
                {
                    "source_table": "review_request",
                    "record_id": review_id,
                    "path": review_row["request_markdown_path"],
                    "expected_sha256": sorted(candidate_markdown_hashes),
                    "observed_sha256": stored_markdown_sha,
                    "reason": "STORED_SHA256_MISMATCH",
                }
            )
        accepted_markdown_hashes = (
            {stored_markdown_sha}
            if isinstance(stored_markdown_sha, str)
            and stored_markdown_sha in candidate_markdown_hashes
            else candidate_markdown_hashes
        )
        try:
            observed_markdown_sha = sha256_file(
                self._absolute(review_row["request_markdown_path"])
            )
        except (OSError, TypeError, ValueError):
            observed_markdown_sha = None
        if (
            observed_markdown_sha is None
            or observed_markdown_sha not in accepted_markdown_hashes
        ):
            issues.append(
                {
                    "source_table": "review_request",
                    "record_id": review_id,
                    "path": review_row["request_markdown_path"],
                    "expected_sha256": sorted(accepted_markdown_hashes),
                    "observed_sha256": observed_markdown_sha,
                    "reason": (
                        "FILE_UNREADABLE"
                        if observed_markdown_sha is None
                        else "SHA256_MISMATCH"
                    ),
                }
            )
        return issues

    @staticmethod
    def _review_lineage(
        connection: sqlite3.Connection,
        *,
        start_review_id: str,
        work_order_id: str,
    ) -> list[sqlite3.Row]:
        """Return direct parent then ancestors, rejecting cycles/cross-work links."""

        lineage: list[sqlite3.Row] = []
        seen: set[str] = set()
        current_id: str | None = start_review_id
        while current_id is not None:
            if current_id in seen:
                raise ValidationError("Review parent lineage contains a cycle")
            seen.add(current_id)
            row = connection.execute(
                "SELECT id, work_order_id, run_id, parent_review_id, status, "
                "schema_version "
                "FROM reviews WHERE id = ?",
                (current_id,),
            ).fetchone()
            if row is None or row["work_order_id"] != work_order_id:
                raise ValidationError("Review parent lineage is invalid")
            lineage.append(row)
            current_id = (
                str(row["parent_review_id"])
                if row["parent_review_id"] is not None
                else None
            )
        return lineage

    def _record_review_integrity_failure(
        self,
        *,
        work_order_id: str,
        venture_id: str,
        run_id: str,
        issues: list[dict[str, Any]],
    ) -> None:
        now = utc_now()
        with self.store.transaction() as connection:
            self.store.append_event(
                "REVIEW_INPUT_TAMPER_DETECTED",
                aggregate_type="WorkOrder",
                aggregate_id=work_order_id,
                venture_id=venture_id,
                payload={
                    "run_id": run_id,
                    "source_table": issues[0]["source_table"],
                    "issues": issues,
                    "detected_at": now,
                },
                connection=connection,
            )

    def _refresh_waiting_review(self, work_order_id: str) -> Review:
        """Reuse a current ReviewRequest or supersede and safely reissue it."""

        source_snapshot = self._source_snapshot()
        replacement_state: str | None = None
        superseded_review_id: str | None = None
        with self.store.transaction() as connection:
            work_row = connection.execute(
                "SELECT * FROM work_orders WHERE id = ?",
                (work_order_id,),
            ).fetchone()
            if work_row is None:
                raise NotFoundError(f"WorkOrder not found: {work_order_id}")
            waiting_row = connection.execute(
                """
                SELECT * FROM reviews
                WHERE work_order_id = ? AND status = 'WAITING_FOR_OPUS'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (work_order_id,),
            ).fetchone()
            if waiting_row is None:
                raise ValidationError("WAITING_FOR_OPUS has no ReviewRequest")
            if work_row["status"] != "WAITING_FOR_OPUS":
                raise ValidationError(
                    "WorkOrder and Review WAITING_FOR_OPUS state is inconsistent"
                )

            reason: str | None = None
            issues: list[dict[str, Any]] = []
            if (
                int(waiting_row["schema_version"]) < 2
                or waiting_row["binding_status"] != "BOUND"
            ):
                reason = "LEGACY_UNBOUND"
            elif (
                waiting_row["source_commit"] != source_snapshot.source_commit
                or waiting_row["source_tree_sha256"]
                != source_snapshot.source_tree_sha256
            ):
                reason = "SOURCE_CHANGED"
            else:
                issues.extend(
                    self._review_request_integrity_issues(waiting_row)
                )
                issues.extend(
                    self._bound_review_material_issues(
                        waiting_row,
                        connection=connection,
                    )
                )
                issues.extend(
                    self._review_resolution_integrity_issues(
                        waiting_row,
                        connection=connection,
                    )
                )
                if issues:
                    reason = "BOUND_INPUT_CHANGED"

            if reason is None:
                return self.review(str(waiting_row["id"]))

            superseded_review_id = str(waiting_row["id"])
            has_parent = waiting_row["parent_review_id"] is not None
            replacement_state = "AWAITING_REREVIEW" if has_parent else "VERIFIED"
            if has_parent and reason == "SOURCE_CHANGED":
                # A new commit after a repair is not covered by the submitted
                # change resolutions. Reopen the direct parent's changes and
                # require a new repair manifest rather than silently carrying
                # old remediation claims onto different source.
                replacement_state = "REPAIR_REQUIRED"
                connection.execute(
                    """
                    UPDATE review_required_changes
                    SET status = 'OPEN', verified_by_review_id = NULL,
                        updated_at = ?
                    WHERE review_id = ? AND status = 'SUBMITTED'
                    """,
                    (utc_now(), waiting_row["parent_review_id"]),
                )
            now = utc_now()
            connection.execute(
                "UPDATE reviews SET status = 'SUPERSEDED', updated_at = ? WHERE id = ?",
                (now, superseded_review_id),
            )
            connection.execute(
                "UPDATE work_orders SET status = ?, updated_at = ? WHERE id = ?",
                (replacement_state, now, work_order_id),
            )
            self.store.append_event(
                "OPUS_REVIEW_REQUEST_SUPERSEDED",
                aggregate_type="Review",
                aggregate_id=superseded_review_id,
                venture_id=str(work_row["venture_id"]),
                payload={
                    "reason": reason,
                    "issues": issues,
                    "replacement_work_order_status": replacement_state,
                    "current_source_commit": source_snapshot.source_commit,
                    "current_source_tree_sha256": (
                        source_snapshot.source_tree_sha256
                    ),
                },
                connection=connection,
            )

        assert superseded_review_id is not None
        assert replacement_state is not None
        if replacement_state == "REPAIR_REQUIRED":
            raise ValidationError(
                "Source changed after repair; the required changes were reopened "
                "and a new repair manifest is required"
            )
        return self.prepare_review(
            work_order_id,
            idempotency_key=(
                f"review-reissue:{work_order_id}:{superseded_review_id}:"
                f"{source_snapshot.source_commit}:"
                f"{source_snapshot.source_tree_sha256}"
            ),
        )

    def prepare_review(
        self,
        work_order_id: str,
        *,
        idempotency_key: str,
    ) -> Review:
        preflight_work = self.work_order(work_order_id)
        if preflight_work.status == "WAITING_FOR_OPUS":
            return self._refresh_waiting_review(work_order_id)
        source_snapshot = self._source_snapshot()
        if preflight_work.status in {"VERIFIED", "AWAITING_REREVIEW"}:
            preflight_run, preflight_evidence, preflight_artifacts = (
                self._review_materials(work_order_id)
            )
            if preflight_run is not None:
                preflight_issues = self._review_integrity_issues(
                    preflight_run,
                    preflight_evidence,
                    preflight_artifacts,
                )
                if preflight_issues:
                    self._record_review_integrity_failure(
                        work_order_id=work_order_id,
                        venture_id=preflight_work.venture_id,
                        run_id=str(preflight_run["id"]),
                        issues=preflight_issues,
                    )
                    raise ValidationError(
                        "review input integrity check failed; Evidence or Artifact "
                        "content no longer matches canonical SQLite metadata"
                    )
        command_payload = {
            "work_order_id": work_order_id,
            "source_commit": source_snapshot.source_commit,
            "source_tree_sha256": source_snapshot.source_tree_sha256,
        }
        created_review_directories: list[Path] = []

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            current_row = connection.execute(
                "SELECT * FROM work_orders WHERE id = ?",
                (work_order_id,),
            ).fetchone()
            if current_row is None:
                raise NotFoundError(f"WorkOrder not found: {work_order_id}")

            waiting_review = connection.execute(
                """
                SELECT id FROM reviews
                WHERE work_order_id = ? AND status = 'WAITING_FOR_OPUS'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (work_order_id,),
            ).fetchone()
            if waiting_review is not None:
                if current_row["status"] != "WAITING_FOR_OPUS":
                    raise ValidationError(
                        "WorkOrder and Review WAITING_FOR_OPUS state is inconsistent"
                    )
                return {"review_id": waiting_review["id"]}
            if current_row["status"] == "WAITING_FOR_OPUS":
                raise ValidationError("WAITING_FOR_OPUS has no ReviewRequest")
            if current_row["status"] not in {"VERIFIED", "AWAITING_REREVIEW"}:
                raise ValidationError(
                    f"Review cannot be prepared from status {current_row['status']}"
                )
            current_source_snapshot = self._source_snapshot()
            if current_source_snapshot != source_snapshot:
                raise ValidationError(
                    "Source snapshot changed while preparing the ReviewRequest"
                )

            current_work = self._work_order_from_row(current_row)
            latest_run, evidence_rows, artifact_rows = self._review_materials(
                work_order_id,
                connection=connection,
            )
            if latest_run is None:
                raise ValidationError("A PASS Run is required before review")
            integrity_issues = self._review_integrity_issues(
                latest_run,
                evidence_rows,
                artifact_rows,
            )
            if integrity_issues:
                raise ValidationError(
                    "review input integrity check failed; Evidence or Artifact "
                    "content no longer matches canonical SQLite metadata"
                )

            review_id = new_id("review")
            directory = contained_path(
                self.root,
                "var",
                "handoffs",
                "reviews",
                work_order_id,
                review_id,
            )
            created_review_directories.append(directory)
            json_path = directory / "opus_review_request.json"
            markdown_path = directory / "opus_review_request.md"
            specification = json.loads(current_row["specification_json"])
            verifier_spec = read_json(current_work.verifier_path)
            verifier_output_row = next(
                (row for row in evidence_rows if row["kind"] == "VERIFIER_OUTPUT"),
                None,
            )
            if verifier_output_row is None:
                raise ValidationError("Verifier output Evidence is required before review")
            verifier_output_path = self._absolute(verifier_output_row["path"])
            verifier_output_content = read_json(verifier_output_path)

            artifact_documents: list[dict[str, Any]] = []
            inline_limit = 64 * 1024
            for artifact_row in artifact_rows:
                artifact_path = self._absolute(artifact_row["path"])
                content_bytes = artifact_path.read_bytes()
                item: dict[str, Any] = {
                    "id": artifact_row["id"],
                    "path": artifact_row["path"],
                    "sha256": artifact_row["sha256"],
                    "media_type": artifact_row["media_type"],
                    "size_bytes": len(content_bytes),
                }
                try:
                    decoded = content_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    decoded = None
                if decoded is not None and len(content_bytes) <= inline_limit:
                    item["content"] = decoded
                    item["inline"] = True
                else:
                    attachment = contained_path(
                        directory,
                        "attachments",
                        f"{artifact_row['id']}_{artifact_path.name}",
                    )
                    attachment.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(artifact_path, attachment)
                    item["inline"] = False
                    item["attachment_path"] = self._relative(attachment)
                    item["attachment_sha256"] = sha256_file(attachment)
                artifact_documents.append(item)

            parent_review_id: str | None = None
            review_lineage: list[str] = []
            change_resolutions: list[dict[str, Any]] = []
            if current_row["status"] == "AWAITING_REREVIEW":
                parent = connection.execute(
                    """
                    SELECT id FROM reviews
                    WHERE work_order_id = ? AND status = 'CHANGES_REQUIRED'
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """,
                    (work_order_id,),
                ).fetchone()
                if parent is None:
                    raise ValidationError(
                        "AWAITING_REREVIEW has no CHANGES_REQUIRED parent Review"
                    )
                parent_review_id = str(parent["id"])
                lineage_rows = self._review_lineage(
                    connection,
                    start_review_id=parent_review_id,
                    work_order_id=work_order_id,
                )
                review_lineage = [str(row["id"]) for row in lineage_rows]
                for lineage_index, lineage_review in enumerate(lineage_rows):
                    lineage_review_id = str(lineage_review["id"])
                    required_rows = connection.execute(
                        """
                        SELECT change_id, description, status
                        FROM review_required_changes
                        WHERE review_id = ? ORDER BY change_id
                        """,
                        (lineage_review_id,),
                    ).fetchall()
                    if not required_rows:
                        continue
                    if any(
                        row["status"] not in {"SUBMITTED", "VERIFIED"}
                        for row in required_rows
                    ):
                        raise ValidationError(
                            "Rereview lineage contains an unresolved required_change"
                        )
                    if lineage_index == 0:
                        resolution_rows = connection.execute(
                            """
                            SELECT required_change_id, repair_run_id, source_commit,
                                   source_tree_sha256, evidence_policy_version,
                                   evidence_json, created_at
                            FROM review_change_resolutions
                            WHERE review_id = ? AND repair_run_id = ?
                            ORDER BY required_change_id, created_at
                            """,
                            (lineage_review_id, latest_run["id"]),
                        ).fetchall()
                    else:
                        child_review = lineage_rows[lineage_index - 1]
                        resolution_rows = connection.execute(
                            """
                            SELECT required_change_id, repair_run_id, source_commit,
                                   source_tree_sha256, evidence_policy_version,
                                   evidence_json, created_at
                            FROM review_change_resolutions
                            WHERE review_id = ? AND repair_run_id = ?
                            ORDER BY required_change_id, created_at
                            """,
                            (lineage_review_id, child_review["run_id"]),
                        ).fetchall()
                    required_ids = [
                        str(row["change_id"]) for row in required_rows
                    ]
                    resolution_ids = [
                        str(row["required_change_id"])
                        for row in resolution_rows
                    ]
                    if required_ids != resolution_ids:
                        raise ValidationError(
                            "Rereview requires one resolution per lineage required_change"
                        )
                    for resolution in resolution_rows:
                        if lineage_index == 0 and (
                            resolution["source_commit"]
                            != current_source_snapshot.source_commit
                            or resolution["source_tree_sha256"]
                            != current_source_snapshot.source_tree_sha256
                        ):
                            raise ValidationError(
                                "Rereview resolution source does not match current source"
                            )
                        evidence_ids = json.loads(resolution["evidence_json"])
                        test_result_required = (
                            int(resolution["evidence_policy_version"]) >= 1
                        )
                        canonical_evidence_ids = self._canonical_repair_evidence_ids(
                            work_order_id,
                            evidence_ids,
                            connection=connection,
                            review_id=lineage_review_id,
                            required_change_id=str(
                                resolution["required_change_id"]
                            ),
                            source_commit=str(resolution["source_commit"]),
                            source_tree_sha256=str(
                                resolution["source_tree_sha256"]
                            ),
                            require_test_result=test_result_required,
                        )
                        required_status = next(
                            row["status"]
                            for row in required_rows
                            if row["change_id"]
                            == resolution["required_change_id"]
                        )
                        change_resolutions.append(
                            {
                                "origin_review_id": lineage_review_id,
                                "required_change_id": resolution[
                                    "required_change_id"
                                ],
                                "description": next(
                                    row["description"]
                                    for row in required_rows
                                    if row["change_id"]
                                    == resolution["required_change_id"]
                                ),
                                "required_change_status": required_status,
                                "repair_run_id": resolution["repair_run_id"],
                                "source_commit": resolution["source_commit"],
                                "source_tree_sha256": resolution[
                                    "source_tree_sha256"
                                ],
                                "evidence_ids": canonical_evidence_ids,
                                "evidence_policy": {
                                    "version": int(
                                        resolution["evidence_policy_version"]
                                    ),
                                    "required_kind": (
                                        "TEST_RESULT"
                                        if test_result_required
                                        else None
                                    ),
                                },
                                "created_at": resolution["created_at"],
                            }
                        )
            requested_evidence_ids = {
                str(row["id"]) for row in evidence_rows
            }
            for resolution in change_resolutions:
                requested_evidence_ids.update(resolution["evidence_ids"])
            evidence_documents: list[dict[str, Any]] = []
            if requested_evidence_ids:
                evidence_placeholders = ",".join(
                    "?" for _ in requested_evidence_ids
                )
                requested_evidence_rows = connection.execute(
                    f"""
                    SELECT id, kind, path, sha256, run_id, trusted, payload_json
                    FROM evidence
                    WHERE id IN ({evidence_placeholders})
                    ORDER BY id
                    """,
                    tuple(sorted(requested_evidence_ids)),
                ).fetchall()
                if len(requested_evidence_rows) != len(requested_evidence_ids):
                    raise ValidationError(
                        "ReviewRequest Evidence metadata is incomplete"
                    )
                for evidence_row in requested_evidence_rows:
                    evidence_path = self._absolute(evidence_row["path"])
                    content_bytes = evidence_path.read_bytes()
                    evidence_document: dict[str, Any] = {
                        "id": evidence_row["id"],
                        "kind": evidence_row["kind"],
                        "path": evidence_row["path"],
                        "sha256": evidence_row["sha256"],
                        "run_id": evidence_row["run_id"],
                        "trusted": bool(evidence_row["trusted"]),
                        "metadata": json.loads(evidence_row["payload_json"]),
                        "size_bytes": len(content_bytes),
                    }
                    try:
                        decoded_evidence = content_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        decoded_evidence = None
                    if (
                        decoded_evidence is not None
                        and len(content_bytes) <= inline_limit
                    ):
                        evidence_document["content"] = decoded_evidence
                        evidence_document["inline"] = True
                    else:
                        evidence_attachment = contained_path(
                            directory,
                            "attachments",
                            (
                                f"evidence_{evidence_row['id']}_"
                                f"{evidence_path.name}"
                            ),
                        )
                        evidence_attachment.parent.mkdir(
                            parents=True,
                            exist_ok=True,
                        )
                        shutil.copyfile(evidence_path, evidence_attachment)
                        evidence_document["inline"] = False
                        evidence_document["attachment_path"] = self._relative(
                            evidence_attachment
                        )
                        evidence_document["attachment_sha256"] = sha256_file(
                            evidence_attachment
                        )
                    evidence_documents.append(evidence_document)
            request_core = {
                "schema_version": 2,
                "review_request_id": review_id,
                "work_order_id": work_order_id,
                "venture_id": current_work.venture_id,
                "run_id": latest_run["id"],
                "parent_review_id": parent_review_id,
                "review_lineage": review_lineage,
                "source_commit": source_snapshot.source_commit,
                "source_tree_oid": source_snapshot.source_tree_oid,
                "source_tree_sha256": source_snapshot.source_tree_sha256,
                "source_dirty": source_snapshot.dirty,
                "requested_reviewer": REVIEWER_METADATA,
                "actual_review_status": "NOT_YET_REVIEWED",
                "review_objectives": [
                    "independent architecture and code review",
                    "hidden-assumption and requirements-gap detection",
                    "verification-weakening and risk review",
                ],
                "work_order": {
                    "id": work_order_id,
                    "title": current_row["title"],
                    "objective": specification.get("objective"),
                    "acceptance": specification.get("acceptance", {}),
                    "specification": specification,
                },
                "verifier": {
                    "path": current_row["verifier_path"],
                    "sha256": current_row["verifier_sha256"],
                    "spec": verifier_spec,
                },
                "verifier_output": {
                    "path": verifier_output_row["path"],
                    "sha256": verifier_output_row["sha256"],
                    "content": verifier_output_content,
                },
                "artifacts": artifact_documents,
                "evidence": evidence_documents,
                "change_resolutions": change_resolutions,
                "response_schema": {
                    "required": [
                        "schema_version",
                        "review_request_id",
                        "review_request_hash",
                        "reviewed_commit",
                        "reviewed_tree_sha256",
                        "source",
                        "verdict",
                        "findings",
                        "required_changes",
                    ],
                    "verdicts": ["PASS", "CHANGES_REQUIRED"],
                    "required_values": {
                        "schema_version": 2,
                        "reviewed_commit": source_snapshot.source_commit,
                        "reviewed_tree_sha256": (
                            source_snapshot.source_tree_sha256
                        ),
                    },
                },
            }
            request_digest = payload_hash(request_core)
            request = {**request_core, "review_request_hash": request_digest}
            atomic_write_json(json_path, request)
            atomic_write_text(
                markdown_path,
                review_request_markdown(request),
            )
            request_json_sha256 = sha256_file(json_path)
            request_markdown_sha256 = sha256_file(markdown_path)
            now = utc_now()
            self.store.insert_row(
                "reviews",
                {
                    "id": review_id,
                    "work_order_id": work_order_id,
                    "run_id": latest_run["id"],
                    "schema_version": 2,
                    "parent_review_id": parent_review_id,
                    "source_commit": source_snapshot.source_commit,
                    "source_tree_sha256": source_snapshot.source_tree_sha256,
                    "binding_status": "BOUND",
                    "status": "WAITING_FOR_OPUS",
                    "request_json_path": self._relative(json_path),
                    "request_markdown_path": self._relative(markdown_path),
                    "response_path": None,
                    "payload_json": self._json(
                        {
                            "request": request,
                            "request_hash": request_digest,
                            "request_json_sha256": request_json_sha256,
                            "request_markdown_sha256": request_markdown_sha256,
                        }
                    ),
                    "created_at": now,
                    "updated_at": now,
                },
                connection=connection,
            )
            connection.execute(
                "UPDATE work_orders SET status = 'WAITING_FOR_OPUS', updated_at = ? WHERE id = ?",
                (now, work_order_id),
            )
            self.store.append_event(
                "OPUS_REVIEW_REQUESTED",
                aggregate_type="WorkOrder",
                aggregate_id=work_order_id,
                venture_id=current_work.venture_id,
                payload={
                    "review_id": review_id,
                    "request_json_path": self._relative(json_path),
                    "request_markdown_path": self._relative(markdown_path),
                    "request_hash": request_digest,
                    "request_json_sha256": request_json_sha256,
                    "request_markdown_sha256": request_markdown_sha256,
                    "source_commit": source_snapshot.source_commit,
                    "source_tree_sha256": source_snapshot.source_tree_sha256,
                    "status": "WAITING_FOR_OPUS",
                },
                connection=connection,
            )
            return {"review_id": review_id}

        try:
            result = self.store.run_idempotent(
                idempotency_key, "prepare_review", command_payload, operation
            )
        except IdempotencyConflict as exc:
            for directory in created_review_directories:
                if directory.exists():
                    shutil.rmtree(directory)
            raise ConflictError(str(exc)) from exc
        except BaseException:
            for directory in created_review_directories:
                if directory.exists():
                    shutil.rmtree(directory)
            raise
        return self.review(str(result["review_id"]))

    def review(self, review_id: str) -> Review:
        row = self._row("reviews", review_id)
        payload = json.loads(row["payload_json"])
        return Review(
            id=row["id"],
            work_order_id=row["work_order_id"],
            status=row["status"],
            binding_status=row["binding_status"],
            json_path=self._absolute(row["request_json_path"]),
            markdown_path=self._absolute(row["request_markdown_path"]),
            request_hash=payload["request_hash"],
            request_markdown_sha256=self._expected_review_markdown_sha256(
                payload
            ),
        )

    def ingest_review_result(
        self, review_id: str, result_file: str | Path
    ) -> dict[str, Any]:
        row = self._row("reviews", review_id)
        review = self.review(review_id)
        integrity_issues = self._review_request_integrity_issues(row)

        if integrity_issues:
            venture_id = self.work_order(review.work_order_id).venture_id
            self.store.append_event(
                "REVIEW_INPUT_TAMPER_DETECTED",
                aggregate_type="Review",
                aggregate_id=review_id,
                venture_id=venture_id,
                payload={
                    "source_table": "review_request",
                    "issues": integrity_issues,
                },
            )
            raise ValidationError(
                "ReviewRequest integrity check failed; the stored request file "
                "does not match its canonical hashes"
            )

        material_issues = self._bound_review_material_issues(row)
        material_issues.extend(self._review_resolution_integrity_issues(row))
        if material_issues:
            venture_id = self.work_order(review.work_order_id).venture_id
            self._record_review_integrity_failure(
                work_order_id=review.work_order_id,
                venture_id=venture_id,
                run_id=str(row["run_id"]),
                issues=material_issues,
            )
            raise ValidationError(
                "ReviewResult cannot be ingested because bound Evidence or "
                "Artifact content changed after the ReviewRequest was created"
            )

        result = read_json(Path(result_file).resolve())
        if not isinstance(result, dict):
            raise ValidationError("ReviewResult must be a JSON object")
        if int(row["schema_version"]) >= 2:
            current_source = self._source_snapshot()
            if (
                current_source.source_commit != row["source_commit"]
                or current_source.source_tree_sha256 != row["source_tree_sha256"]
            ):
                venture_id = self.work_order(review.work_order_id).venture_id
                self.store.append_event(
                    "STALE_REVIEW_RESULT_REJECTED",
                    aggregate_type="Review",
                    aggregate_id=review_id,
                    venture_id=venture_id,
                    payload={
                        "requested_commit": row["source_commit"],
                        "current_commit": current_source.source_commit,
                        "requested_tree_sha256": row["source_tree_sha256"],
                        "current_tree_sha256": current_source.source_tree_sha256,
                    },
                )
                raise ValidationError(
                    "ReviewResult is stale because the current source snapshot changed"
                )
        validate_review_result(
            result,
            request_id=review.id,
            request_hash=review.request_hash,
            request_schema_version=int(row["schema_version"]),
            source_commit=row["source_commit"] if int(row["schema_version"]) >= 2 else None,
            source_tree_sha256=(
                row["source_tree_sha256"] if int(row["schema_version"]) >= 2 else None
            ),
            allow_fake_reviewer=self.allow_test_reviewers,
        )
        if int(row["schema_version"]) < 2 and result["verdict"] == "PASS":
            raise ValidationError(
                "Legacy unbound ReviewRequest cannot complete a WorkOrder; "
                "create a schema-v2 rereview"
            )
        command_payload = {
            "review_id": review_id,
            "result_hash": payload_hash(result),
        }
        response_target = contained_path(
            review.json_path.parent,
            "opus_review_response.json",
        )

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            current_row = connection.execute(
                "SELECT * FROM reviews WHERE id = ?",
                (review_id,),
            ).fetchone()
            if current_row is None:
                raise NotFoundError(f"Review not found: {review_id}")
            if current_row["status"] != "WAITING_FOR_OPUS":
                raise ValidationError(
                    f"ReviewResult cannot be ingested from status {current_row['status']}"
                )
            current_material_issues = self._bound_review_material_issues(
                current_row,
                connection=connection,
            )
            current_material_issues.extend(
                self._review_request_integrity_issues(current_row)
            )
            current_material_issues.extend(
                self._review_resolution_integrity_issues(
                    current_row,
                    connection=connection,
                )
            )
            if current_material_issues:
                raise ValidationError(
                    "ReviewResult is stale because bound Evidence or Artifact "
                    "content changed during ingest"
                )
            if int(current_row["schema_version"]) >= 2:
                commit_snapshot = self._source_snapshot()
                if (
                    commit_snapshot.source_commit != current_row["source_commit"]
                    or commit_snapshot.source_tree_sha256
                    != current_row["source_tree_sha256"]
                ):
                    raise ValidationError(
                        "ReviewResult is stale because source changed during ingest"
                    )
            atomic_write_json(response_target, result)
            current_payload = json.loads(current_row["payload_json"])
            current_payload["result"] = result
            current_payload["result_hash"] = command_payload["result_hash"]
            current_payload["response_json_sha256"] = sha256_file(response_target)
            new_review_status = (
                "CHANGES_REQUIRED"
                if result["verdict"] == "CHANGES_REQUIRED"
                else "COMPLETED"
            )
            new_work_status = (
                "REPAIR_REQUIRED"
                if result["verdict"] == "CHANGES_REQUIRED"
                else "COMPLETED"
            )
            now = utc_now()
            connection.execute(
                """
                UPDATE reviews
                SET status = ?, response_path = ?, payload_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_review_status,
                    self._relative(response_target),
                    self._json(current_payload),
                    now,
                    review_id,
                ),
            )
            connection.execute(
                "UPDATE work_orders SET status = ?, updated_at = ? WHERE id = ?",
                (new_work_status, now, review.work_order_id),
            )
            venture_id = self.work_order(review.work_order_id).venture_id
            if result["required_changes"]:
                for change in result["required_changes"]:
                    self.store.insert_row(
                        "review_required_changes",
                        {
                            "id": new_id("review_change"),
                            "review_id": review_id,
                            "change_id": change["id"],
                            "description": change["description"],
                            "status": "OPEN",
                            "verified_by_review_id": None,
                            "created_at": now,
                            "updated_at": now,
                        },
                        connection=connection,
                    )
                self.store.insert_row(
                    "decisions",
                    {
                        "id": new_id("decision"),
                        "venture_id": venture_id,
                        "work_order_id": review.work_order_id,
                        "status": "ACTION_REQUIRED",
                        "payload_json": self._json(
                            {
                                "classification": "DECISION",
                                "source": result["source"],
                                "required_changes": result["required_changes"],
                            }
                        ),
                        "created_at": now,
                        "updated_at": now,
                    },
                    connection=connection,
                )
            if result["verdict"] == "PASS" and current_row["parent_review_id"]:
                parent_review_id = str(current_row["parent_review_id"])
                lineage = self._review_lineage(
                    connection,
                    start_review_id=parent_review_id,
                    work_order_id=review.work_order_id,
                )
                lineage_ids = [str(item["id"]) for item in lineage]
                placeholders = ",".join("?" for _ in lineage_ids)
                unresolved_count = int(
                    connection.execute(
                        f"""
                        SELECT COUNT(*) FROM review_required_changes
                        WHERE review_id IN ({placeholders})
                          AND status NOT IN ('SUBMITTED', 'VERIFIED')
                        """,
                        tuple(lineage_ids),
                    ).fetchone()[0]
                )
                if unresolved_count:
                    raise ValidationError(
                        "Rereview PASS cannot verify a lineage with unresolved changes"
                    )
                connection.execute(
                    f"""
                    UPDATE review_required_changes
                    SET status = 'VERIFIED', verified_by_review_id = ?, updated_at = ?
                    WHERE review_id IN ({placeholders}) AND status = 'SUBMITTED'
                    """,
                    (review_id, now, *lineage_ids),
                )
                connection.execute(
                    """
                    UPDATE decisions SET status = 'RESOLVED', updated_at = ?
                    WHERE work_order_id = ? AND status = 'ACTION_REQUIRED'
                    """,
                    (now, review.work_order_id),
                )
            self.store.append_event(
                "REVIEW_RESULT_INGESTED",
                aggregate_type="Review",
                aggregate_id=review_id,
                venture_id=venture_id,
                payload={
                    "verdict": result["verdict"],
                    "source": result["source"],
                    "actual_reviewer_claim_not_verified": True,
                },
                connection=connection,
            )
            return {"result": result}

        try:
            stored = self.store.run_idempotent(
                f"review-ingest:{review_id}",
                "ingest_review_result",
                command_payload,
                operation,
            )
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc
        return dict(stored["result"])

    def _canonical_repair_evidence_ids(
        self,
        work_order_id: str,
        evidence_ids: Any,
        *,
        connection: sqlite3.Connection | None = None,
        review_id: str | None = None,
        required_change_id: str | None = None,
        source_commit: str | None = None,
        source_tree_sha256: str | None = None,
        require_test_result: bool = False,
    ) -> list[str]:
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise ValidationError("Repair change requires canonical Evidence ids")
        if not all(
            isinstance(evidence_id, str) and evidence_id.strip()
            for evidence_id in evidence_ids
        ):
            raise ValidationError("Repair Evidence ids must be non-empty strings")
        normalized = [str(evidence_id) for evidence_id in evidence_ids]
        if len(normalized) != len(set(normalized)):
            raise ValidationError("Repair Evidence ids must be unique")

        conn = connection or self.store.connection
        found_test_result = False
        for evidence_id in normalized:
            row = conn.execute(
                """
                SELECT evidence.id, evidence.work_order_id, evidence.run_id,
                       evidence.kind, evidence.path, evidence.sha256,
                       evidence.trusted, evidence.payload_json,
                       runs.work_order_id AS run_work_order_id,
                       runs.status AS run_status
                FROM evidence
                LEFT JOIN runs ON runs.id = evidence.run_id
                WHERE evidence.id = ?
                """,
                (evidence_id,),
            ).fetchone()
            if row is None:
                raise ValidationError(
                    f"Repair Evidence is not a canonical record: {evidence_id}"
                )
            if row["work_order_id"] != work_order_id:
                raise ValidationError(
                    f"Repair Evidence is not linked to this WorkOrder: {evidence_id}"
                )
            if int(row["trusted"]) != 1:
                raise ValidationError(
                    f"Repair Evidence must be trusted: {evidence_id}"
                )
            issue = self._file_integrity_issue(
                source_table="evidence",
                record_id=evidence_id,
                stored_path=row["path"],
                expected_sha256=row["sha256"],
            )
            if issue is not None:
                raise ValidationError(
                    f"Repair Evidence content is missing or changed: {evidence_id}"
                )
            if row["kind"] == "TEST_RESULT":
                found_test_result = True
                if row["run_id"] is not None:
                    raise ValidationError(
                        f"TEST_RESULT must not masquerade as Run Evidence: {evidence_id}"
                    )
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValidationError(
                        f"TEST_RESULT metadata is invalid: {evidence_id}"
                    ) from exc
                nodes = payload.get("test_node_ids") if isinstance(payload, dict) else None
                if (
                    not isinstance(payload, dict)
                    or payload.get("schema_version") != 1
                    or payload.get("trusted") is not True
                    or payload.get("trust_basis")
                    != "LOCAL_TEST_RESULT_REGISTRATION"
                    or payload.get("result_file_sha256") != row["sha256"]
                    or payload.get("receipt_kind") != "PYTEST_RESULT"
                    or payload.get("receipt_status") != "PASSED"
                    or type(payload.get("receipt_exit_code")) is not int
                    or payload.get("receipt_exit_code") != 0
                    or not isinstance(nodes, list)
                    or not nodes
                    or not all(isinstance(node, str) and node.strip() for node in nodes)
                    or len(nodes) != len(set(nodes))
                ):
                    raise ValidationError(
                        f"TEST_RESULT metadata is incomplete or inconsistent: {evidence_id}"
                    )
                validate_test_result_receipt(
                    self._absolute(str(row["path"])).read_bytes(),
                    selected_node_ids=nodes,
                    source_commit=str(payload.get("source_commit")),
                    source_tree_sha256=str(
                        payload.get("source_tree_sha256")
                    ),
                )
                expected_values = (
                    ("review_id", review_id),
                    ("required_change_id", required_change_id),
                    ("source_commit", source_commit),
                    ("source_tree_sha256", source_tree_sha256),
                )
                for field, expected in expected_values:
                    if expected is not None and payload.get(field) != expected:
                        raise ValidationError(
                            f"TEST_RESULT {field} does not match repair change: {evidence_id}"
                        )
            elif (
                row["run_id"] is None
                or row["run_work_order_id"] != work_order_id
                or row["run_status"] != "PASS"
            ):
                raise ValidationError(
                    f"Repair Evidence must be TEST_RESULT or trusted PASS Run Evidence: {evidence_id}"
                )
        if require_test_result and not found_test_result:
            raise ValidationError(
                "Each repair change requires change-specific TEST_RESULT Evidence"
            )
        return sorted(normalized)

    def _validate_repair_manifest(
        self,
        work_order_id: str,
        repair_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(repair_manifest, dict):
            raise ValidationError("Repair manifest must be a JSON object")
        required_fields = {
            "schema_version",
            "review_id",
            "source_commit",
            "source_tree_sha256",
            "changes",
        }
        missing = sorted(required_fields.difference(repair_manifest))
        if missing:
            raise ValidationError(
                "Repair manifest missing fields: " + ", ".join(missing)
            )
        if repair_manifest["schema_version"] != 1:
            raise ValidationError("Unsupported repair manifest schema_version")
        review_row = self.store.query_one(
            """
            SELECT * FROM reviews
            WHERE id = ? AND work_order_id = ? AND status = 'CHANGES_REQUIRED'
            """,
            (repair_manifest["review_id"], work_order_id),
        )
        if review_row is None:
            raise ValidationError(
                "Repair manifest must reference the CHANGES_REQUIRED Review"
            )
        current_work = self.work_order(work_order_id)
        if current_work.status != "REPAIR_REQUIRED":
            raise ValidationError("WorkOrder is not awaiting repair")
        latest_review = self.store.query_one(
            """
            SELECT id, status FROM reviews
            WHERE work_order_id = ? AND status = 'CHANGES_REQUIRED'
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (work_order_id,),
        )
        if (
            latest_review is None
            or latest_review["id"] != review_row["id"]
        ):
            raise ValidationError(
                "Repair manifest must reference the latest CHANGES_REQUIRED Review"
            )
        snapshot = self._source_snapshot()
        if repair_manifest["source_commit"] != snapshot.source_commit:
            raise ValidationError("Repair manifest source_commit does not match source")
        if repair_manifest["source_tree_sha256"] != snapshot.source_tree_sha256:
            raise ValidationError(
                "Repair manifest source_tree_sha256 does not match source"
            )
        changes = repair_manifest["changes"]
        if not isinstance(changes, list):
            raise ValidationError("Repair manifest changes must be a list")
        expected_rows = self.store.query_all(
            """
            SELECT change_id, status FROM review_required_changes
            WHERE review_id = ? ORDER BY change_id
            """,
            (review_row["id"],),
        )
        expected_ids = [str(row["change_id"]) for row in expected_rows]
        if not expected_rows or any(row["status"] != "OPEN" for row in expected_rows):
            raise ValidationError(
                "Latest Review required changes are not all open for repair"
            )
        observed_ids = [
            str(change.get("id")) for change in changes if isinstance(change, dict)
        ]
        if sorted(observed_ids) != expected_ids or len(set(observed_ids)) != len(
            observed_ids
        ):
            raise ValidationError(
                "Repair manifest must cover every required_change id exactly once"
            )

        normalized_changes: list[dict[str, Any]] = []
        for change in changes:
            if change.get("commit") != snapshot.source_commit:
                raise ValidationError(
                    f"Repair change {change.get('id')} commit does not match source"
                )
            normalized_evidence_ids = self._canonical_repair_evidence_ids(
                work_order_id,
                change.get("evidence_ids"),
                review_id=str(review_row["id"]),
                required_change_id=str(change["id"]),
                source_commit=snapshot.source_commit,
                source_tree_sha256=snapshot.source_tree_sha256,
                require_test_result=True,
            )
            normalized_changes.append(
                {
                    "id": str(change["id"]),
                    "commit": snapshot.source_commit,
                    "evidence_ids": normalized_evidence_ids,
                }
            )

        if int(review_row["schema_version"]) >= 2:
            prior_payload = json.loads(review_row["payload_json"])
            prior_request = prior_payload.get("request", {})
            prior_artifacts = {
                item.get("path"): item.get("sha256")
                for item in prior_request.get("artifacts", [])
                if isinstance(item, dict)
            }
            current_artifacts: dict[str, str] = {}
            for artifact in self.store.query_all(
                "SELECT path FROM artifacts WHERE work_order_id = ? ORDER BY path",
                (work_order_id,),
            ):
                path = self._absolute(artifact["path"])
                if path.is_file():
                    current_artifacts[str(artifact["path"])] = sha256_file(path)
            if (
                review_row["source_tree_sha256"] == snapshot.source_tree_sha256
                and prior_artifacts == current_artifacts
            ):
                work = self.work_order(work_order_id)
                self.store.append_event(
                    "REPAIR_NO_CHANGE_DETECTED",
                    aggregate_type="WorkOrder",
                    aggregate_id=work_order_id,
                    venture_id=work.venture_id,
                    payload={"review_id": review_row["id"]},
                )
                raise ValidationError("Repair has no substantive change to rereview")

        return {
            "schema_version": 1,
            "review_id": str(review_row["id"]),
            "source_commit": snapshot.source_commit,
            "source_tree_sha256": snapshot.source_tree_sha256,
            "changes": normalized_changes,
        }

    @staticmethod
    def _repair_manifest_replay_identity(
        repair_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Project a manifest to the immutable identity used for replay checks."""

        if not isinstance(repair_manifest, dict):
            raise ValidationError("Repair manifest must be a JSON object")
        changes = repair_manifest.get("changes")
        if not isinstance(changes, list):
            raise ValidationError("Repair manifest changes must be a list")
        projected_changes: list[dict[str, Any]] = []
        for change in changes:
            if not isinstance(change, dict):
                raise ValidationError("Repair manifest change must be an object")
            evidence_ids = change.get("evidence_ids")
            if not isinstance(evidence_ids, list):
                raise ValidationError("Repair change requires canonical Evidence ids")
            projected_changes.append(
                {
                    "id": change.get("id"),
                    "commit": change.get("commit"),
                    "evidence_ids": sorted(str(item) for item in evidence_ids),
                }
            )
        return {
            "schema_version": repair_manifest.get("schema_version"),
            "review_id": repair_manifest.get("review_id"),
            "source_commit": repair_manifest.get("source_commit"),
            "source_tree_sha256": repair_manifest.get("source_tree_sha256"),
            "changes": sorted(projected_changes, key=lambda item: str(item["id"])),
        }

    def repair_once(
        self,
        work_order_id: str,
        *,
        executor: ExecutorPort,
        idempotency_key: str,
        repair_manifest: dict[str, Any] | None = None,
    ) -> Run:
        if repair_manifest is None:
            raise ValidationError("WorkOrder repair requires a repair manifest")
        existing = self.store.get_row("idempotency", idempotency_key)
        if existing is not None and existing["status"] == "COMPLETED":
            if existing["command"] != "repair_work_order":
                raise ConflictError(
                    "Idempotency key belongs to a different command"
                )
            stored_request = json.loads(existing["request_json"])
            current_work = self.work_order(work_order_id)
            if (
                stored_request.get("work_order_id") != work_order_id
                or stored_request.get("executor") != executor.name
                or stored_request.get("approved_verifier_hash")
                != current_work.verifier_hash
                or stored_request.get("repair_mode") is not True
            ):
                raise ConflictError(
                    "Idempotency key was reused with different repair inputs"
                )
            stored_result = json.loads(existing["result_json"])
            replayed = self.run(str(stored_result["run_id"]))
            run_row = self._row("runs", replayed.id)
            run_payload = json.loads(run_row["payload_json"])
            stored_manifest = run_payload.get("repair_manifest")
            supplied_identity = self._repair_manifest_replay_identity(
                repair_manifest
            )
            if not isinstance(stored_manifest, dict) or payload_hash(
                supplied_identity
            ) != payload_hash(
                self._repair_manifest_replay_identity(stored_manifest)
            ):
                raise ConflictError(
                    "Idempotency key was reused with a different repair manifest"
                )
            return replayed
        normalized_manifest = self._validate_repair_manifest(
            work_order_id,
            repair_manifest,
        )
        repaired = self._execute_work_order(
            work_order_id,
            executor=executor,
            idempotency_key=idempotency_key,
            _repair_mode=True,
            _repair_manifest=normalized_manifest,
        )
        if (
            repaired.status == "PASS"
            and self.work_order(work_order_id).status == "AWAITING_REREVIEW"
        ):
            self.prepare_review(
                work_order_id,
                idempotency_key=f"rereview:{work_order_id}:{repaired.id}",
            )
        return repaired

    def stop(self) -> None:
        if self.store.is_stopped():
            return
        with self.store.transaction() as connection:
            self.store.set_stopped(True, connection=connection)
            self.store.append_event(
                "COMPANY_STOPPED",
                aggregate_type="Company",
                aggregate_id="global",
                payload={"stopped": True},
                connection=connection,
            )

    def resume(self) -> None:
        if not self.store.is_stopped():
            return
        with self.store.transaction() as connection:
            self.store.set_stopped(False, connection=connection)
            self.store.append_event(
                "COMPANY_RESUMED",
                aggregate_type="Company",
                aggregate_id="global",
                payload={"stopped": False},
                connection=connection,
            )

    def is_stopped(self) -> bool:
        return self.store.is_stopped()

    def resume_work_order(
        self,
        work_order_id: str,
        *,
        executor: ExecutorPort,
        repair_manifest: dict[str, Any] | None = None,
    ) -> Review | Run:
        if self.is_stopped():
            raise CompanyStoppedError("Company execution is stopped; run company resume")
        work_order = self.work_order(work_order_id)
        if work_order.status == "WAITING_FOR_OPUS":
            return self.prepare_review(
                work_order_id,
                idempotency_key=f"refresh-review:{work_order_id}",
            )
        if work_order.status == "VERIFIED":
            review_count = int(
                self.store.scalar(
                    "SELECT COUNT(*) FROM reviews WHERE work_order_id = ?",
                    (work_order_id,),
                )
            )
            return self.prepare_review(
                work_order_id,
                idempotency_key=f"resume-review:{work_order_id}:{review_count}",
            )
        if work_order.status == "AWAITING_REREVIEW":
            latest_run = self.store.query_one(
                """
                SELECT id FROM runs
                WHERE work_order_id = ? AND status = 'PASS'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (work_order_id,),
            )
            if latest_run is None:
                raise ValidationError("AWAITING_REREVIEW has no PASS repair Run")
            return self.prepare_review(
                work_order_id,
                idempotency_key=f"rereview:{work_order_id}:{latest_run['id']}",
            )
        next_attempt = len(self.runs_for_work_order(work_order_id)) + 1
        if work_order.status == "REPAIR_REQUIRED":
            return self.repair_once(
                work_order_id,
                executor=executor,
                idempotency_key=f"resume-repair:{work_order_id}:attempt:{next_attempt}",
                repair_manifest=repair_manifest,
            )
        if work_order.status in {"READY", "VERIFICATION_FAILED"}:
            return self.execute_work_order(
                work_order_id,
                executor=executor,
                idempotency_key=(
                    f"resume-execute:{work_order_id}:attempt:{next_attempt}"
                ),
            )
        raise ValidationError(f"No resumable action for status {work_order.status}")

    def assumptions_for_venture(self, venture_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.store.query_all(
                "SELECT * FROM assumptions WHERE venture_id = ? ORDER BY id",
                (venture_id,),
            )
        ]

    def decisions_for_venture(self, venture_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.store.query_all(
                "SELECT * FROM decisions WHERE venture_id = ? ORDER BY id",
                (venture_id,),
            )
        ]

    def events(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in self.store.events_for():
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def inbox(self) -> dict[str, Any]:
        decisions = [
            dict(row)
            for row in self.store.query_all(
                """
                SELECT * FROM decisions
                WHERE status IN ('PENDING', 'ACTION_REQUIRED')
                ORDER BY created_at, id
                """
            )
        ]
        approvals = [
            dict(row)
            for row in self.store.query_all(
                """
                SELECT * FROM approvals
                WHERE status NOT IN ('APPROVED', 'NOT_REQUIRED')
                ORDER BY created_at, id
                """
            )
        ]
        file_items: list[dict[str, Any]] = []
        inbox_root = contained_path(self.root, "var", "inbox")
        if inbox_root.is_dir():
            for path in sorted(inbox_root.rglob("*.json")):
                try:
                    payload = read_json(path)
                except (OSError, UnicodeError, json.JSONDecodeError):
                    payload = {"type": "UNREADABLE_INBOX_ITEM"}
                file_items.append(
                    {"path": self._relative(path), "payload": payload}
                )
        return {
            "decisions": decisions,
            "approvals": approvals,
            "file_items": file_items,
        }

    def export_event_ledger(self, destination: str | Path) -> Path:
        return self.store.export_events_jsonl(destination)
