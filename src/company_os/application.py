from __future__ import annotations

import json
import re
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    CompanyStoppedError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from .first_principles import FirstPrinciplesGate, GateResult
from .handoffs import (
    MODEL_METADATA,
    REVIEWER_METADATA,
    markdown_document,
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
from .storage import (
    IdempotencyConflict,
    SQLiteStateStore,
    canonical_json,
    new_id,
    utc_now,
)
from .utils import (
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
_ROLES = ("cto", "cpo", "cmo")


class ExistingArtifactExecutor:
    """Adapter for an artifact created outside the program by this session."""

    name = "current_cursor_codex_session"

    def execute(self, work_order: WorkOrder, workspace_path: Path) -> ExecutionOutput:
        path = contained_path(workspace_path, work_order.artifact_relative_path)
        if not path.is_file():
            raise ValidationError(f"Expected externally-created artifact is missing: {path}")
        return ExecutionOutput(artifact_path=path)


class CompanyOS:
    def __init__(self, root: str | Path, db_path: str | Path | None = None):
        self.root = Path(root).resolve()
        default_db = self.root / "var" / "state" / "company.db"
        self.db_path = Path(db_path).resolve() if db_path else default_db
        self.store = SQLiteStateStore(self.db_path)
        self.gate = FirstPrinciplesGate()

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

    def prepare_council(self, idea_id: str) -> tuple[CouncilRequest, ...]:
        idea = self.idea(idea_id)
        command_payload = {"idea_id": idea_id, "schema_version": 1}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            directory = contained_path(
                self.root, "var", "handoffs", "council", idea_id
            )
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
                (utc_now(), idea_id),
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
            f"council-prepare:{idea_id}:v1",
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
        target = contained_path(
            self.root,
            "var",
            "handoffs",
            "council",
            idea_id,
            f"{normalized_role}_response.json",
        )
        command_payload = {
            "idea_id": idea_id,
            "role": normalized_role,
            "response_hash": payload_hash(payload),
        }

        def operation(connection: sqlite3.Connection) -> dict[str, str]:
            atomic_write_json(target, payload)
            response_id = new_id("council_response")
            now = utc_now()
            self.store.insert_row(
                "council_responses",
                {
                    "id": response_id,
                    "idea_id": idea_id,
                    "role": normalized_role,
                    "payload_json": self._json(payload),
                    "source_path": self._relative(target),
                    "created_at": now,
                    "updated_at": now,
                },
                connection=connection,
            )
            self.store.append_event(
                "COUNCIL_RESPONSE_INGESTED",
                aggregate_type="Idea",
                aggregate_id=idea_id,
                payload={
                    "role": normalized_role,
                    "response_hash": command_payload["response_hash"],
                },
                connection=connection,
            )
            return {"response_path": self._relative(target)}

        try:
            result = self.store.run_idempotent(
                f"council-ingest:{idea_id}:{normalized_role}",
                "ingest_council_response",
                command_payload,
                operation,
            )
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc
        return self._absolute(str(result["response_path"]))

    def compile_council(
        self,
        idea_id: str,
        *,
        idempotency_key: str,
    ) -> CompileOutcome:
        self.idea(idea_id)
        rows = self.store.query_all(
            "SELECT * FROM council_responses WHERE idea_id = ? ORDER BY role",
            (idea_id,),
        )
        by_role = {row["role"]: json.loads(row["payload_json"]) for row in rows}
        missing = [role for role in _ROLES if role not in by_role]
        if missing:
            raise ValidationError(
                f"Council responses are incomplete; missing: {', '.join(missing)}"
            )
        contributions = {
            role: by_role[role]["contract_contribution"] for role in _ROLES
        }
        fingerprints = {role: payload_hash(value) for role, value in contributions.items()}
        if len(set(fingerprints.values())) != 1:
            inbox_path = contained_path(
                self.root, "var", "inbox", "ideas", idea_id, "council_conflict.json"
            )
            atomic_write_json(
                inbox_path,
                {
                    "type": "COUNCIL_CONTRIBUTION_CONFLICT",
                    "idea_id": idea_id,
                    "role_contribution_hashes": fingerprints,
                    "status": "CEO_DECISION_REQUIRED",
                },
            )
            self.store.append_event(
                "COUNCIL_CONFLICT_DETECTED",
                aggregate_type="Idea",
                aggregate_id=idea_id,
                payload={"inbox_path": self._relative(inbox_path), "hashes": fingerprints},
            )
            raise ConflictError(f"Council contributions conflict; see {inbox_path}")

        contract = deepcopy(contributions["cto"])
        contract["council_provenance"] = {
            "compiler": "deterministic_fixed_mapping_v1",
            "roles": list(_ROLES),
            "contribution_hashes": fingerprints,
            "agreement_is_not_evidence": True,
        }
        gate_result = self.gate.validate(contract)
        command_payload = {
            "idea_id": idea_id,
            "response_hashes": {
                role: payload_hash(by_role[role]) for role in _ROLES
            },
        }

        def operation(connection: sqlite3.Connection) -> dict[str, str]:
            encoded_contract = self._json(contract)
            existing_contract = connection.execute(
                """
                SELECT id FROM contracts
                WHERE idea_id = ? AND payload_json = ?
                ORDER BY created_at LIMIT 1
                """,
                (idea_id, encoded_contract),
            ).fetchone()
            if existing_contract is not None:
                return {"contract_id": existing_contract["id"]}
            contract_id = new_id("contract")
            now = utc_now()
            self.store.insert_row(
                "contracts",
                {
                    "id": contract_id,
                    "idea_id": idea_id,
                    "decision_level": contract["decision_level"],
                    "gate_status": "PASSED" if gate_result.passed else "FAILED",
                    "payload_json": encoded_contract,
                    "created_at": now,
                    "updated_at": now,
                },
                connection=connection,
            )
            connection.execute(
                "UPDATE ideas SET status = ?, updated_at = ? WHERE id = ?",
                (
                    "CONTRACT_COMPILED" if gate_result.passed else "GATE_FAILED",
                    now,
                    idea_id,
                ),
            )
            self.store.append_event(
                "FIRST_PRINCIPLES_GATE_EVALUATED",
                aggregate_type="VentureContract",
                aggregate_id=contract_id,
                payload={
                    "passed": gate_result.passed,
                    "violations": [
                        {"code": v.code, "field": v.field, "message": v.message}
                        for v in gate_result.violations
                    ],
                },
                connection=connection,
            )
            return {"contract_id": contract_id}

        try:
            result = self.store.run_idempotent(
                idempotency_key, "compile_council", command_payload, operation
            )
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc
        contract_id = str(result["contract_id"])
        stored_contract = json.loads(self._row("contracts", contract_id)["payload_json"])
        return CompileOutcome(
            contract_id=contract_id,
            gate_result=self.gate.validate(stored_contract),
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
        if contract_row["gate_status"] != "PASSED":
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

        def operation(connection: sqlite3.Connection) -> dict[str, str]:
            venture_id = new_id("venture")
            metric_id = new_id("metric")
            experiment_id = new_id("experiment")
            work_order_id = new_id("work_order")
            decision_id = new_id("decision")
            approval_id = new_id("approval")
            now = utc_now()

            workspace = self.venture_workspace(venture_id)
            manifest_path = workspace / "context_manifest.json"
            verifier_path = workspace / "work_orders" / work_order_id / "verifier.json"
            artifact_relative_path = "artifacts/verified_result.txt"
            expected_content = f"synthetic verified evidence for {idea_id}\n"
            approved_verifier_hash = write_exact_text_verifier(
                verifier_path,
                artifact_relative_path=artifact_relative_path,
                expected_content=expected_content,
            )

            assumption_claims = [
                claim
                for claim in contract.get("claims", [])
                if claim.get("type") == "ASSUMPTION"
            ]
            assumption_ids = [str(item["id"]) for item in assumption_claims]
            source_fact = next(
                (
                    claim
                    for claim in contract.get("claims", [])
                    if claim.get("type") == "FACT"
                ),
                None,
            )
            source_evidence_id = (
                str(source_fact["evidence_refs"][0])
                if source_fact and source_fact.get("evidence_refs")
                else new_id("source_evidence")
            )
            source_response = contained_path(
                self.root,
                "var",
                "handoffs",
                "council",
                idea_id,
                "cto_response.json",
            )

            manifest = {
                "schema_version": 1,
                "venture_id": venture_id,
                "idea_id": idea_id,
                "contract_id": contract_id,
                "decision_level": contract["decision_level"],
                "approval_status": normalized_approval,
                "assumption_ids": assumption_ids,
                "metric_ids": [metric_id],
                "experiment_ids": [experiment_id],
                "decision_ids": [decision_id],
                "work_order_ids": [work_order_id],
                "source_evidence_ids": [source_evidence_id],
                "workspace_policy": "VENTURE_ONLY",
                "created_at": now,
            }
            atomic_write_json(manifest_path, manifest)

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
                        {"context_isolation": "VENTURE_ONLY"}
                    ),
                    "created_at": now,
                    "updated_at": now,
                },
                connection=connection,
            )
            for claim in assumption_claims:
                self.store.insert_row(
                    "assumptions",
                    {
                        "id": str(claim["id"]),
                        "venture_id": venture_id,
                        "contract_id": contract_id,
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
            self.store.insert_row(
                "approvals",
                {
                    "id": approval_id,
                    "contract_id": contract_id,
                    "decision_id": None,
                    "status": normalized_approval,
                    "actor": "CEO" if normalized_approval == "APPROVED" else "SYSTEM_POLICY",
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
            if source_response.is_file():
                self.store.insert_row(
                    "evidence",
                    {
                        "id": source_evidence_id,
                        "venture_id": venture_id,
                        "work_order_id": None,
                        "run_id": None,
                        "kind": "SOURCE_EVIDENCE",
                        "path": self._relative(source_response),
                        "sha256": sha256_file(source_response),
                        "payload_json": self._json(
                            {
                                "trusted": True,
                                "source_types": (
                                    source_fact.get("source_types", [])
                                    if source_fact
                                    else []
                                ),
                            }
                        ),
                        "created_at": now,
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
            return {"venture_id": venture_id}

        try:
            result = self.store.run_idempotent(
                idempotency_key,
                "record_approval_and_scaffold",
                command_payload,
                operation,
            )
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc
        return self.venture(str(result["venture_id"]))

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
        _repair_mode: bool = False,
    ) -> Run:
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
                        }
                    ),
                    "started_at": now,
                    "finished_at": finished,
                    "created_at": now,
                },
                connection=connection,
            )
            artifact_sha = sha256_file(artifact)
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
                    self._json({"synthetic": True}),
                    finished,
                ),
            )
            for kind, path, digest in (
                ("RUN_ARTIFACT", artifact, artifact_sha),
                (
                    "VERIFIER_OUTPUT",
                    verifier_output_path,
                    sha256_file(verifier_output_path),
                ),
            ):
                self.store.insert_row(
                    "evidence",
                    {
                        "id": new_id("evidence"),
                        "venture_id": venture.id,
                        "work_order_id": work_order_id,
                        "run_id": run_id,
                        "kind": kind,
                        "path": self._relative(path),
                        "sha256": digest,
                        "payload_json": self._json(
                            {
                                "trusted": True,
                                "verification_status": verification.status,
                            }
                        ),
                        "created_at": finished,
                    },
                    connection=connection,
                )
            if _repair_mode:
                next_work_status = (
                    "REPAIRED_VERIFIED"
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
                connection.execute(
                    """
                    UPDATE reviews SET status = 'CHANGES_APPLIED', updated_at = ?
                    WHERE id = (
                        SELECT id FROM reviews WHERE work_order_id = ?
                        ORDER BY created_at DESC LIMIT 1
                    )
                    """,
                    (finished, work_order_id),
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
                "venture_id": venture.id,
                "work_order_id": work_order.id,
                "run_id": run_id,
                "kind": "UNTRUSTED_ARTIFACT" if artifact_path else "TAMPERED_VERIFIER",
                "path": self._relative(evidence_path),
                "sha256": evidence_digest,
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
                "venture_id": venture.id,
                "work_order_id": work_order.id,
                "run_id": run_id,
                "kind": "UNTRUSTED_ARTIFACT",
                "path": self._relative(artifact_path),
                "sha256": sha256_file(artifact_path) if artifact_path.is_file() else None,
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

    def prepare_review(
        self,
        work_order_id: str,
        *,
        idempotency_key: str,
    ) -> Review:
        command_payload = {"work_order_id": work_order_id}

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
            if current_row["status"] != "VERIFIED":
                raise ValidationError(
                    f"Review cannot be prepared from status {current_row['status']}"
                )

            current_work = self._work_order_from_row(current_row)
            latest_run = connection.execute(
                """
                SELECT * FROM runs
                WHERE work_order_id = ? AND status = 'PASS'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (work_order_id,),
            ).fetchone()
            if latest_run is None:
                raise ValidationError("A PASS Run is required before review")
            evidence_rows = connection.execute(
                """
                SELECT id, kind, path, sha256 FROM evidence
                WHERE run_id = ? ORDER BY id
                """,
                (latest_run["id"],),
            ).fetchall()
            artifact_rows = connection.execute(
                """
                SELECT id, path, sha256 FROM artifacts
                WHERE run_id = ? ORDER BY id
                """,
                (latest_run["id"],),
            ).fetchall()

            integrity_issues: list[dict[str, Any]] = []
            if not evidence_rows:
                integrity_issues.append(
                    {
                        "source_table": "evidence",
                        "record_id": latest_run["id"],
                        "path": None,
                        "expected_sha256": None,
                        "observed_sha256": None,
                        "reason": "METADATA_MISSING",
                    }
                )
            if not artifact_rows:
                integrity_issues.append(
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
                ("evidence", evidence_rows),
                ("artifacts", artifact_rows),
            ):
                for row in rows:
                    issue = self._file_integrity_issue(
                        source_table=source_table,
                        record_id=str(row["id"]),
                        stored_path=row["path"],
                        expected_sha256=row["sha256"],
                    )
                    if issue is not None:
                        integrity_issues.append(issue)

            if integrity_issues:
                now = utc_now()
                self.store.append_event(
                    "REVIEW_INPUT_TAMPER_DETECTED",
                    aggregate_type="WorkOrder",
                    aggregate_id=work_order_id,
                    venture_id=current_work.venture_id,
                    payload={
                        "run_id": latest_run["id"],
                        "source_table": integrity_issues[0]["source_table"],
                        "issues": integrity_issues,
                        "detected_at": now,
                    },
                    connection=connection,
                )
                return {
                    "integrity_error": (
                        "review input integrity check failed; Evidence or Artifact "
                        "content no longer matches canonical SQLite metadata"
                    ),
                    "issues": integrity_issues,
                }

            review_id = new_id("review")
            directory = contained_path(
                self.root,
                "var",
                "handoffs",
                "reviews",
                work_order_id,
                review_id,
            )
            json_path = directory / "opus_review_request.json"
            markdown_path = directory / "opus_review_request.md"
            request_core = {
                "schema_version": 1,
                "review_request_id": review_id,
                "work_order_id": work_order_id,
                "venture_id": current_work.venture_id,
                "run_id": latest_run["id"],
                "requested_reviewer": REVIEWER_METADATA,
                "actual_review_status": "NOT_YET_REVIEWED",
                "review_objectives": [
                    "independent architecture and code review",
                    "hidden-assumption and requirements-gap detection",
                    "verification-weakening and risk review",
                ],
                "evidence": [dict(row) for row in evidence_rows],
                "response_schema": {
                    "required": [
                        "schema_version",
                        "review_request_id",
                        "review_request_hash",
                        "source",
                        "verdict",
                        "findings",
                        "required_changes",
                    ],
                    "verdicts": ["PASS", "CHANGES_REQUIRED"],
                },
            }
            request_digest = payload_hash(request_core)
            request = {**request_core, "review_request_hash": request_digest}
            atomic_write_json(json_path, request)
            atomic_write_text(
                markdown_path,
                markdown_document("Claude Opus ReviewRequest", request),
            )
            request_json_sha256 = sha256_file(json_path)
            now = utc_now()
            self.store.insert_row(
                "reviews",
                {
                    "id": review_id,
                    "work_order_id": work_order_id,
                    "run_id": latest_run["id"],
                    "status": "WAITING_FOR_OPUS",
                    "request_json_path": self._relative(json_path),
                    "request_markdown_path": self._relative(markdown_path),
                    "response_path": None,
                    "payload_json": self._json(
                        {
                            "request": request,
                            "request_hash": request_digest,
                            "request_json_sha256": request_json_sha256,
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
            raise ConflictError(str(exc)) from exc
        if "integrity_error" in result:
            raise ValidationError(str(result["integrity_error"]))
        return self.review(str(result["review_id"]))

    def review(self, review_id: str) -> Review:
        row = self._row("reviews", review_id)
        payload = json.loads(row["payload_json"])
        return Review(
            id=row["id"],
            work_order_id=row["work_order_id"],
            status=row["status"],
            json_path=self._absolute(row["request_json_path"]),
            markdown_path=self._absolute(row["request_markdown_path"]),
            request_hash=payload["request_hash"],
        )

    def ingest_review_result(
        self, review_id: str, result_file: str | Path
    ) -> dict[str, Any]:
        row = self._row("reviews", review_id)
        review = self.review(review_id)
        stored_payload = json.loads(row["payload_json"])
        integrity_issues: list[dict[str, Any]] = []
        expected_file_sha = stored_payload.get("request_json_sha256")
        try:
            observed_file_sha = sha256_file(review.json_path)
        except OSError:
            observed_file_sha = None
        if not isinstance(expected_file_sha, str) or (
            observed_file_sha != expected_file_sha
        ):
            integrity_issues.append(
                {
                    "source_table": "review_request",
                    "record_id": review_id,
                    "path": self._relative(review.json_path),
                    "expected_sha256": expected_file_sha,
                    "observed_sha256": observed_file_sha,
                    "reason": (
                        "FILE_UNREADABLE"
                        if observed_file_sha is None
                        else "SHA256_MISMATCH"
                    ),
                }
            )

        actual_request: Any = None
        try:
            actual_request = read_json(review.json_path)
        except (OSError, UnicodeError, json.JSONDecodeError):
            integrity_issues.append(
                {
                    "source_table": "review_request",
                    "record_id": review_id,
                    "path": self._relative(review.json_path),
                    "reason": "INVALID_JSON",
                }
            )
        if isinstance(actual_request, dict):
            embedded_hash = actual_request.get("review_request_hash")
            request_core = dict(actual_request)
            request_core.pop("review_request_hash", None)
            observed_payload_hash = payload_hash(request_core)
            if (
                embedded_hash != review.request_hash
                or observed_payload_hash != review.request_hash
                or actual_request != stored_payload.get("request")
            ):
                integrity_issues.append(
                    {
                        "source_table": "review_request",
                        "record_id": review_id,
                        "path": self._relative(review.json_path),
                        "expected_payload_hash": review.request_hash,
                        "embedded_payload_hash": embedded_hash,
                        "observed_payload_hash": observed_payload_hash,
                        "reason": "PAYLOAD_HASH_MISMATCH",
                    }
                )
        elif actual_request is not None:
            integrity_issues.append(
                {
                    "source_table": "review_request",
                    "record_id": review_id,
                    "path": self._relative(review.json_path),
                    "reason": "REQUEST_NOT_OBJECT",
                }
            )

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

        result = read_json(Path(result_file).resolve())
        if not isinstance(result, dict):
            raise ValidationError("ReviewResult must be a JSON object")
        validate_review_result(
            result,
            request_id=review.id,
            request_hash=review.request_hash,
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

    def repair_once(
        self,
        work_order_id: str,
        *,
        executor: ExecutorPort,
        idempotency_key: str,
    ) -> Run:
        current_status = self.work_order(work_order_id).status
        existing = self.store.get_row("idempotency", idempotency_key)
        if current_status == "REPAIRED_VERIFIED" and existing is not None:
            stored_result = json.loads(existing["result_json"])
            return self.run(stored_result["run_id"])
        if current_status != "REPAIR_REQUIRED":
            raise ValidationError("WorkOrder is not awaiting repair")
        return self.execute_work_order(
            work_order_id,
            executor=executor,
            idempotency_key=idempotency_key,
            _repair_mode=True,
        )

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
        self, work_order_id: str, *, executor: ExecutorPort
    ) -> Review | Run:
        if self.is_stopped():
            raise CompanyStoppedError("Company execution is stopped; run company resume")
        work_order = self.work_order(work_order_id)
        if work_order.status == "WAITING_FOR_OPUS":
            row = self.store.query_one(
                "SELECT id FROM reviews WHERE work_order_id = ? ORDER BY created_at DESC LIMIT 1",
                (work_order_id,),
            )
            if row is None:
                raise ValidationError("WAITING_FOR_OPUS has no ReviewRequest")
            return self.review(row["id"])
        if work_order.status == "VERIFIED":
            return self.prepare_review(
                work_order_id,
                idempotency_key=f"resume-review:{work_order_id}",
            )
        next_attempt = len(self.runs_for_work_order(work_order_id)) + 1
        if work_order.status == "REPAIR_REQUIRED":
            return self.repair_once(
                work_order_id,
                executor=executor,
                idempotency_key=f"resume-repair:{work_order_id}:attempt:{next_attempt}",
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
