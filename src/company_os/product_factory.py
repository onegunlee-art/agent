"""Hash-bound product design, scenarios, DAG planning, and CEO approval."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from .errors import ConflictError, NotFoundError, ValidationError
from .storage import IdempotencyConflict, canonical_json, new_id, utc_now
from .utils import atomic_write_json, contained_path, read_json, sha256_file


_PRODUCT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_GIT_REF = re.compile(r"^(?!.*\.\.)(?!/)(?!.*//)[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_ARTIFACT_FIELDS = {
    "product_brief": ("product_brief_path", "product_brief_sha256"),
    "development_schema": (
        "development_schema_path",
        "development_schema_sha256",
    ),
    "user_scenarios": ("user_scenarios_path", "user_scenarios_sha256"),
    "implementation_plan": (
        "implementation_plan_path",
        "implementation_plan_sha256",
    ),
    "approval_bundle": ("approval_bundle_path", "approval_bundle_sha256"),
}


class ProductFactory:
    def __init__(self, company) -> None:
        self.company = company

    def draft(
        self,
        session_id: str,
        *,
        definition_file: str | Path,
        idempotency_key: str,
    ) -> dict[str, Any]:
        definition = self._read_definition(definition_file)
        ordered_plan = self._validate_definition(definition)
        product_id = definition["product_id"]
        definition_sha = sha256(
            canonical_json(definition).encode("utf-8")
        ).hexdigest()
        command_payload = {
            "session_id": session_id,
            "product_id": product_id,
            "definition_sha256": definition_sha,
        }
        created_paths: list[Path] = []
        try:
            with self.company.store.transaction() as connection:
                claim = self.company.store.claim_idempotency(
                    idempotency_key,
                    "draft_product_approval_bundle",
                    command_payload,
                    connection=connection,
                )
                if not claim.is_new:
                    if claim.completed and isinstance(claim.result, dict):
                        return dict(claim.result)
                    raise ConflictError("Product draft with this key is still in progress")
                session = connection.execute(
                    "SELECT * FROM council_sessions WHERE id=?", (session_id,)
                ).fetchone()
                if session is None:
                    raise NotFoundError(f"Council session not found: {session_id}")
                completed_turns = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM council_turns WHERE session_id=? AND status='COMPLETED'",
                        (session_id,),
                    ).fetchone()[0]
                )
                if session["status"] != "CLOSED" or completed_turns < 2:
                    raise ValidationError(
                        "Product planning requires a closed council session with at least two completed turns"
                    )
                product = connection.execute(
                    "SELECT * FROM products WHERE id=?", (product_id,)
                ).fetchone()
                now = utc_now()
                if product is None:
                    self.company.store.insert_row(
                        "products",
                        {
                            "id": product_id,
                            "session_id": session_id,
                            "status": "PLANNING",
                            "current_bundle_id": None,
                            "created_at": now,
                            "updated_at": now,
                        },
                        connection=connection,
                    )
                elif product["session_id"] != session_id:
                    raise ValidationError("Product ID is already bound to a different council session")
                version = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(version), 0) + 1 FROM product_bundles WHERE product_id=?",
                        (product_id,),
                    ).fetchone()[0]
                )
                bundle_id = new_id("product_bundle")
                base = contained_path(
                    self.company.root,
                    "var",
                    "products",
                    product_id,
                    "bundles",
                    f"v{version:04d}",
                )
                document_values = {
                    "product_brief": definition["product_brief"],
                    "development_schema": definition["development_schema"],
                    "user_scenarios": definition["user_scenarios"],
                    "implementation_plan": ordered_plan,
                }
                artifact_records: dict[str, dict[str, str]] = {}
                for name, content in document_values.items():
                    path = base / f"{name.replace('_', '-')}.json"
                    atomic_write_json(
                        path,
                        {
                            "schema_version": 1,
                            "kind": name.replace("_", "-"),
                            "product_id": product_id,
                            "version": version,
                            "content": content,
                        },
                    )
                    created_paths.append(path)
                    artifact_records[name] = {
                        "path": self.company._relative(path),
                        "sha256": sha256_file(path),
                    }
                approval_path = base / "approval-bundle.json"
                approval_content = {
                    "schema_version": 1,
                    "kind": "approval-bundle",
                    "bundle_id": bundle_id,
                    "product_id": product_id,
                    "version": version,
                    "council_session_id": session_id,
                    "artifacts": artifact_records,
                    "target": definition["target"],
                    "delivery_scope": {
                        "source_push": bool(definition["target"]["push"]),
                        "merge_to_main": bool(definition["target"]["merge_to_main"]),
                        "server_deployment": False,
                    },
                }
                atomic_write_json(approval_path, approval_content)
                created_paths.append(approval_path)
                artifact_records["approval_bundle"] = {
                    "path": self.company._relative(approval_path),
                    "sha256": sha256_file(approval_path),
                }
                row_values = {
                    "id": bundle_id,
                    "product_id": product_id,
                    "version": version,
                    "status": "DRAFT",
                    "target_json": canonical_json(definition["target"]),
                    "created_at": now,
                    "updated_at": now,
                }
                for name, (path_field, hash_field) in _ARTIFACT_FIELDS.items():
                    row_values[path_field] = artifact_records[name]["path"]
                    row_values[hash_field] = artifact_records[name]["sha256"]
                self.company.store.insert_row(
                    "product_bundles", row_values, connection=connection
                )
                for index, item in enumerate(ordered_plan):
                    self.company.store.insert_row(
                        "product_work_orders",
                        {
                            "id": new_id("product_work_order"),
                            "bundle_id": bundle_id,
                            "work_key": item["key"],
                            "title": item["title"],
                            "order_index": index,
                            "dependencies_json": canonical_json(item["depends_on"]),
                            "specification_json": canonical_json(item),
                            "status": "READY" if not item["depends_on"] else "BLOCKED",
                            "created_at": now,
                            "updated_at": now,
                        },
                        connection=connection,
                    )
                connection.execute(
                    "UPDATE products SET current_bundle_id=?, status='PLANNING', updated_at=? WHERE id=?",
                    (bundle_id, now, product_id),
                )
                self.company.store.append_event(
                    "PRODUCT_APPROVAL_BUNDLE_DRAFTED",
                    aggregate_type="ProductBundle",
                    aggregate_id=bundle_id,
                    correlation_id=session_id,
                    payload={
                        "product_id": product_id,
                        "version": version,
                        "bundle_sha256": artifact_records["approval_bundle"]["sha256"],
                        "work_order_count": len(ordered_plan),
                        "scenario_count": len(definition["user_scenarios"]),
                        "server_deployment": False,
                    },
                    connection=connection,
                )
                result = self._bundle_view_from_values(
                    bundle_id=bundle_id,
                    product_id=product_id,
                    version=version,
                    status="DRAFT",
                    target=definition["target"],
                    artifacts=artifact_records,
                    work_orders=ordered_plan,
                )
                self.company.store.complete_idempotency(
                    idempotency_key,
                    result,
                    command="draft_product_approval_bundle",
                    connection=connection,
                )
                return result
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc
        except BaseException:
            for path in reversed(created_paths):
                path.unlink(missing_ok=True)
            raise

    def approval_text(self, bundle_id: str) -> str:
        row = self._bundle(bundle_id)
        target = json.loads(row["target_json"])
        delivery = "main 병합과 push" if target["merge_to_main"] else "승인된 브랜치 push"
        return (
            f"제품 {row['product_id']}의 사용자 시나리오·개발 설계·작업계획 "
            f"v{int(row['version'])} (approval bundle SHA-256: "
            f"{row['approval_bundle_sha256']})을 승인합니다. "
            f"대상은 {target['remote_url']}#{target['delivery_ref']}이며 {delivery}까지 승인하고, "
            "서버 배포는 제외합니다."
        )

    def approve(
        self,
        bundle_id: str,
        *,
        expected_sha256: str,
        approval_file: str | Path,
        idempotency_key: str,
    ) -> dict[str, Any]:
        row = self._bundle(bundle_id)
        if row["status"] != "DRAFT":
            existing = self.company.store.query_one(
                "SELECT * FROM product_approvals WHERE bundle_id=?", (bundle_id,)
            )
            if existing is not None:
                return self._approval_view(existing)
            raise ValidationError("Only a DRAFT product bundle can be approved")
        self._verify_bundle_files(row, after_approval=False)
        if expected_sha256 != row["approval_bundle_sha256"]:
            raise ValidationError("Approval bundle SHA-256 does not match")
        source = Path(approval_file)
        if not source.is_file():
            raise ValidationError("CEO approval file not found")
        approval_text = source.read_text(encoding="utf-8").strip()
        if approval_text != self.approval_text(bundle_id):
            raise ValidationError("CEO approval sentence does not match the current bundle")
        payload = {
            "bundle_id": bundle_id,
            "bundle_sha256": expected_sha256,
            "approval_sha256": sha256(approval_text.encode("utf-8")).hexdigest(),
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            current = connection.execute(
                "SELECT * FROM product_bundles WHERE id=?", (bundle_id,)
            ).fetchone()
            if current is None or current["status"] != "DRAFT":
                raise ValidationError("Product bundle changed before CEO approval")
            approval_id = new_id("product_approval")
            now = utc_now()
            inserted = self.company.store.insert_row(
                "product_approvals",
                {
                    "id": approval_id,
                    "product_id": current["product_id"],
                    "bundle_id": bundle_id,
                    "status": "APPROVED",
                    "actor": "CEO",
                    "approval_text": approval_text,
                    "approval_sha256": payload["approval_sha256"],
                    "bundle_sha256": expected_sha256,
                    "created_at": now,
                },
                connection=connection,
            )
            connection.execute(
                "UPDATE product_bundles SET status='APPROVED', updated_at=? WHERE id=?",
                (now, bundle_id),
            )
            connection.execute(
                "UPDATE products SET status='APPROVED', current_bundle_id=?, updated_at=? WHERE id=?",
                (bundle_id, now, current["product_id"]),
            )
            session = connection.execute(
                "SELECT s.idea_id FROM products p JOIN council_sessions s ON s.id=p.session_id WHERE p.id=?",
                (current["product_id"],),
            ).fetchone()
            assert session is not None
            self.company.store.insert_row(
                "evidence",
                {
                    "id": new_id("evidence"),
                    "idea_id": session["idea_id"],
                    "venture_id": None,
                    "work_order_id": None,
                    "run_id": None,
                    "external_ref": f"product-approval:{bundle_id}",
                    "kind": "PRODUCT_APPROVAL_BUNDLE",
                    "path": current["approval_bundle_path"],
                    "sha256": expected_sha256,
                    "trusted": 1,
                    "payload_json": canonical_json(
                        {
                            "approval_id": approval_id,
                            "product_id": current["product_id"],
                            "bundle_id": bundle_id,
                            "actor": "CEO",
                            "approval_sha256": payload["approval_sha256"],
                            "supports_external_fact": False,
                        }
                    ),
                    "created_at": now,
                },
                connection=connection,
            )
            self.company.store.append_event(
                "PRODUCT_PLAN_APPROVED",
                aggregate_type="ProductApproval",
                aggregate_id=approval_id,
                correlation_id=bundle_id,
                payload={
                    "product_id": current["product_id"],
                    "bundle_id": bundle_id,
                    "bundle_sha256": expected_sha256,
                    "approval_sha256": payload["approval_sha256"],
                    "target": json.loads(current["target_json"]),
                    "server_deployment": False,
                },
                connection=connection,
            )
            return self._approval_view(inserted)

        try:
            return self.company.store.run_idempotent(
                idempotency_key, "approve_product_plan", payload, operation
            )
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc

    def execution_plan(self, product_id: str, bundle_id: str) -> list[dict[str, Any]]:
        row = self._bundle(bundle_id)
        if row["product_id"] != product_id:
            raise ValidationError("Approval bundle belongs to a different product")
        approval = self.company.store.query_one(
            "SELECT * FROM product_approvals WHERE bundle_id=? AND product_id=?",
            (bundle_id, product_id),
        )
        if approval is None or row["status"] != "APPROVED":
            raise ValidationError("Product execution requires hash-bound CEO approval")
        self._verify_bundle_files(row, after_approval=True)
        if approval["bundle_sha256"] != row["approval_bundle_sha256"]:
            raise ValidationError("Product approval is bound to a different bundle version")
        rows = self.company.store.query_all(
            "SELECT * FROM product_work_orders WHERE bundle_id=? ORDER BY order_index",
            (bundle_id,),
        )
        return [json.loads(item["specification_json"]) for item in rows]

    def bundle(self, bundle_id: str) -> dict[str, Any]:
        row = self._bundle(bundle_id)
        rows = self.company.store.query_all(
            "SELECT * FROM product_work_orders WHERE bundle_id=? ORDER BY order_index",
            (bundle_id,),
        )
        artifacts = {
            name: {"path": row[path_field], "sha256": row[hash_field]}
            for name, (path_field, hash_field) in _ARTIFACT_FIELDS.items()
        }
        return self._bundle_view_from_values(
            bundle_id=bundle_id,
            product_id=row["product_id"],
            version=int(row["version"]),
            status=row["status"],
            target=json.loads(row["target_json"]),
            artifacts=artifacts,
            work_orders=[json.loads(item["specification_json"]) for item in rows],
        )

    def _bundle(self, bundle_id: str) -> sqlite3.Row:
        row = self.company.store.get_row("product_bundles", bundle_id)
        if row is None:
            raise NotFoundError(f"Product bundle not found: {bundle_id}")
        return row

    def _verify_bundle_files(self, row: sqlite3.Row, *, after_approval: bool) -> None:
        for name, (path_field, hash_field) in _ARTIFACT_FIELDS.items():
            path = self.company._absolute(row[path_field])
            if not path.is_file() or sha256_file(path) != row[hash_field]:
                suffix = " changed after CEO approval" if after_approval else " does not match its recorded hash"
                raise ValidationError(f"{name}{suffix}")

    def _bundle_view_from_values(
        self,
        *,
        bundle_id: str,
        product_id: str,
        version: int,
        status: str,
        target: dict[str, Any],
        artifacts: dict[str, dict[str, str]],
        work_orders: list[dict[str, Any]],
    ) -> dict[str, Any]:
        rendered = {}
        for name, item in artifacts.items():
            absolute = self.company._absolute(item["path"])
            rendered[name] = {
                "path": item["path"],
                "absolute_path": str(absolute),
                "sha256": item["sha256"],
            }
        return {
            "bundle_id": bundle_id,
            "product_id": product_id,
            "version": version,
            "status": status,
            "bundle_sha256": rendered["approval_bundle"]["sha256"],
            "target": target,
            "artifacts": rendered,
            "work_orders": work_orders,
        }

    @staticmethod
    def _approval_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "approval_id": row["id"],
            "product_id": row["product_id"],
            "bundle_id": row["bundle_id"],
            "status": row["status"],
            "actor": row["actor"],
            "approval_sha256": row["approval_sha256"],
            "bundle_sha256": row["bundle_sha256"],
        }

    @staticmethod
    def _read_definition(path: str | Path) -> dict[str, Any]:
        source = Path(path)
        if not source.is_file():
            raise ValidationError(f"Product definition file not found: {source}")
        try:
            value = read_json(source)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError("Product definition must be UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("Product definition must be an object")
        return value

    @staticmethod
    def _validate_definition(value: dict[str, Any]) -> list[dict[str, Any]]:
        required = {
            "schema_version",
            "product_id",
            "target",
            "product_brief",
            "development_schema",
            "user_scenarios",
            "implementation_plan",
        }
        if set(value) != required or value.get("schema_version") != 1:
            raise ValidationError("Product definition schema is invalid")
        product_id = value.get("product_id")
        if not isinstance(product_id, str) or not _PRODUCT_ID.fullmatch(product_id):
            raise ValidationError("Product ID must be a lowercase hyphenated identifier")
        for name in ("product_brief", "development_schema"):
            if not isinstance(value.get(name), dict) or not value[name]:
                raise ValidationError(f"{name} must be a non-empty object")
        scenarios = value.get("user_scenarios")
        if not isinstance(scenarios, list) or not scenarios:
            raise ValidationError("At least one concrete user scenario is required")
        scenario_ids: set[str] = set()
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                raise ValidationError("Each user scenario must be an object")
            fields = {"id", "actor", "start", "actions", "observations", "error_cases"}
            if set(scenario) != fields:
                raise ValidationError("User scenario fields are incomplete")
            if not all(scenario.get(field) for field in fields):
                raise ValidationError("User scenario values must not be empty")
            if scenario["id"] in scenario_ids:
                raise ValidationError("User scenario IDs must be unique")
            scenario_ids.add(scenario["id"])
        target = value.get("target")
        required_target = {
            "repository",
            "remote_url",
            "base_ref",
            "delivery_ref",
            "merge_to_main",
            "push",
            "server_deployment",
        }
        if not isinstance(target, dict) or set(target) != required_target:
            raise ValidationError("GitHub target is incomplete")
        remote = target.get("remote_url")
        if (
            not isinstance(remote, str)
            or not remote.startswith("https://github.com/")
            or "@" in remote
            or "?" in remote
        ):
            raise ValidationError("GitHub remote must be an approved credential-free HTTPS URL")
        for name in ("base_ref", "delivery_ref"):
            if not isinstance(target.get(name), str) or not _GIT_REF.fullmatch(target[name]):
                raise ValidationError(f"Git {name} is invalid")
        if target.get("server_deployment") is not False:
            raise ValidationError("Server deployment must be explicitly excluded")
        if not isinstance(target.get("merge_to_main"), bool) or not isinstance(target.get("push"), bool):
            raise ValidationError("Git delivery flags must be boolean")
        plan = value.get("implementation_plan")
        if not isinstance(plan, list) or len(plan) < 2:
            raise ValidationError("Implementation plan requires at least two WorkOrders")
        by_key: dict[str, dict[str, Any]] = {}
        fields = {
            "key",
            "title",
            "depends_on",
            "allowed_files",
            "tests",
            "completion_criteria",
        }
        for item in plan:
            if not isinstance(item, dict) or set(item) != fields:
                raise ValidationError("Implementation WorkOrder fields are incomplete")
            key = item.get("key")
            if not isinstance(key, str) or not _PRODUCT_ID.fullmatch(key):
                raise ValidationError("Implementation WorkOrder key is invalid")
            if key in by_key:
                raise ValidationError("Implementation WorkOrder keys must be unique")
            if not isinstance(item.get("depends_on"), list):
                raise ValidationError("WorkOrder dependencies must be a list")
            if not isinstance(item.get("completion_criteria"), list) or not item["completion_criteria"]:
                raise ValidationError("Every WorkOrder requires completion criteria")
            for list_name in ("allowed_files", "tests"):
                if not isinstance(item.get(list_name), list) or not item[list_name]:
                    raise ValidationError(f"Every WorkOrder requires {list_name}")
            by_key[key] = item
        for key, item in by_key.items():
            for dependency in item["depends_on"]:
                if dependency not in by_key:
                    raise ValidationError(f"WorkOrder {key} has unresolved dependency {dependency}")
                if dependency == key:
                    raise ValidationError("Implementation plan contains a dependency cycle")
        ordered: list[dict[str, Any]] = []
        remaining = list(by_key)
        completed: set[str] = set()
        while remaining:
            ready = [key for key in remaining if set(by_key[key]["depends_on"]) <= completed]
            if not ready:
                raise ValidationError("Implementation plan contains a dependency cycle")
            for key in ready:
                ordered.append(by_key[key])
                completed.add(key)
                remaining.remove(key)
        return ordered


__all__ = ["ProductFactory"]
