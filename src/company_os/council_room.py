"""Durable, multi-turn executive council sessions.

SQLite owns session, turn, execution, and decision state.  Larger immutable
messages are stored as hash-bound local artifacts.  Provider processes are
invoked only after the short execution-claim transaction has committed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
from typing import Any, Mapping, Protocol, TYPE_CHECKING

from .errors import (
    CompanyStoppedError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from .roles import serialize_role_spec
from .storage import IdempotencyConflict, canonical_json, new_id, utc_now
from .utils import atomic_write_json, atomic_write_text, contained_path, sha256_file

if TYPE_CHECKING:
    from .application import CompanyOS


ROLE_NAMES = ("cto", "cpo", "cmo")
DEFAULT_ROLE_ROUTES = {"cto": "codex", "cpo": "claude", "cmo": "codex"}
RETRYABLE_STATUSES = {
    "QUOTA_WAIT",
    "AUTH_REQUIRED",
    "TIMEOUT",
    "INVALID_RESPONSE",
    "CLI_NOT_FOUND",
    "CLI_FAILED",
    "EXPIRED",
}
_MAX_MESSAGE_BYTES = 128 * 1024


@dataclass(frozen=True, slots=True)
class ExecutiveRequest:
    session_id: str
    turn_id: str
    turn_number: int
    role: str
    provider: str
    prompt: str
    workspace: Path
    frozen_input_json: str
    frozen_input_sha256: str
    time_limit_seconds: int


@dataclass(frozen=True, slots=True)
class ExecutiveOutcome:
    status: str
    response: dict[str, Any] | None
    provider: str
    model: str | None
    usage: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    error: str | None = None
    raw_output: str = ""


class ExecutiveRunner(Protocol):
    def run(self, request: ExecutiveRequest) -> ExecutiveOutcome: ...


_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": 1},
        "role": {"enum": list(ROLE_NAMES)},
        "speech": {"type": "string", "minLength": 1},
        "contribution": {"type": "object"},
        "questions": {"type": "array", "items": {"type": ["string", "object"]}},
        "advisory": {"type": "array", "items": {"type": ["string", "object"]}},
        "evidence_refs": {"type": "array", "items": {"type": ["string", "object"]}},
        "unresolved_decisions": {
            "type": "array",
            "items": {"type": ["string", "object"]},
        },
    },
    "required": [
        "schema_version",
        "role",
        "speech",
        "contribution",
        "questions",
        "advisory",
        "evidence_refs",
        "unresolved_decisions",
    ],
}


class SubscriptionExecutiveRunner:
    """Run one subscription CLI with no tools and a read-only workspace."""

    _SAFE_ENVIRONMENT = (
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "LANG",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
    )

    def __init__(
        self,
        *,
        codex_executable: str = "codex",
        claude_executable: str = "claude",
        process_runner: Any | None = None,
    ) -> None:
        self.codex_executable = codex_executable
        self.claude_executable = claude_executable
        self._process_runner = process_runner or self._run_process
        self._resolve_executables = process_runner is None

    def run(self, request: ExecutiveRequest) -> ExecutiveOutcome:
        started = time.monotonic()
        request.workspace.mkdir(parents=True, exist_ok=True)
        schema_path = request.workspace / f"{request.turn_id}-{request.role}-schema.json"
        output_path = request.workspace / f"{request.turn_id}-{request.role}-response.json"
        atomic_write_json(schema_path, _RESPONSE_SCHEMA)
        output_path.unlink(missing_ok=True)
        environment = {
            key: value for key, value in os.environ.items() if key in self._SAFE_ENVIRONMENT
        }
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        if request.provider == "codex":
            executable = self._resolve(self.codex_executable, provider="codex")
            command = [
                executable,
                "exec",
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--json",
                "-",
            ]
        elif request.provider == "claude":
            executable = self._resolve(self.claude_executable, provider="claude")
            command = [
                executable,
                "-p",
                "--output-format",
                "json",
                "--json-schema",
                canonical_json(_RESPONSE_SCHEMA),
                "--permission-mode",
                "dontAsk",
                "--permission-prompts",
                "none",
                "--tools",
                "",
                "--effort",
                "max",
                "--no-session-persistence",
                request.prompt,
            ]
        else:
            raise ValidationError(f"Unsupported executive provider: {request.provider}")
        try:
            completed = self._process_runner(
                command,
                cwd=request.workspace,
                environment=environment,
                timeout=request.time_limit_seconds,
                input_text=request.prompt if request.provider == "codex" else None,
            )
        except FileNotFoundError as exc:
            return ExecutiveOutcome(
                "CLI_NOT_FOUND",
                None,
                request.provider,
                None,
                duration_seconds=time.monotonic() - started,
                error=str(exc),
            )
        except subprocess.TimeoutExpired:
            return ExecutiveOutcome(
                "TIMEOUT",
                None,
                request.provider,
                None,
                duration_seconds=time.monotonic() - started,
                error=f"time limit {request.time_limit_seconds}s exceeded",
            )
        combined = f"{completed.stdout}\n{completed.stderr}"
        if completed.returncode != 0:
            return ExecutiveOutcome(
                self._failure_status(combined),
                None,
                request.provider,
                None,
                duration_seconds=time.monotonic() - started,
                error=f"{request.provider} CLI exit={completed.returncode}",
                raw_output=combined[-8000:],
            )
        try:
            if request.provider == "codex":
                response = json.loads(output_path.read_text(encoding="utf-8"))
                usage: dict[str, int] = {}
                model = None
                for line in completed.stdout.splitlines():
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(event, dict):
                        if isinstance(event.get("model"), str):
                            model = event["model"]
                        if event.get("type") == "turn.completed" and isinstance(
                            event.get("usage"), dict
                        ):
                            usage = {
                                key: int(value)
                                for key, value in event["usage"].items()
                                if isinstance(value, (int, float))
                            }
            else:
                envelope = json.loads(completed.stdout)
                response = envelope.get("structured_output")
                model = envelope.get("model") if isinstance(envelope.get("model"), str) else None
                raw_usage = envelope.get("usage")
                usage = (
                    {
                        key: int(value)
                        for key, value in raw_usage.items()
                        if isinstance(value, (int, float))
                    }
                    if isinstance(raw_usage, dict)
                    else {}
                )
            response = _validate_response(response, request.role)
        except (OSError, json.JSONDecodeError, ValidationError, TypeError) as exc:
            return ExecutiveOutcome(
                "INVALID_RESPONSE",
                None,
                request.provider,
                None,
                duration_seconds=time.monotonic() - started,
                error=str(exc),
                raw_output=combined[-8000:],
            )
        return ExecutiveOutcome(
            "COMPLETED",
            response,
            request.provider,
            model,
            usage=usage,
            duration_seconds=time.monotonic() - started,
            raw_output=combined[-8000:],
        )

    def _resolve(self, configured: str, *, provider: str) -> str:
        if not self._resolve_executables:
            return configured
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        located = shutil.which(configured)
        if located:
            return located
        if provider == "claude":
            fallback = Path.home() / ".local" / "bin" / (
                "claude.exe" if os.name == "nt" else "claude"
            )
            if fallback.is_file():
                return str(fallback)
        raise FileNotFoundError(f"{provider} subscription CLI not found: {configured}")

    @staticmethod
    def _failure_status(output: str) -> str:
        value = output.casefold()
        if any(marker in value for marker in ("not logged in", "login required", "authentication required", "unauthorized")):
            return "AUTH_REQUIRED"
        if any(marker in value for marker in ("quota", "rate limit", "usage limit", "resets at")):
            return "QUOTA_WAIT"
        return "CLI_FAILED"

    @staticmethod
    def _run_process(
        command: list[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout: int,
        input_text: str | None,
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            else:
                process.kill()
            process.communicate(timeout=10)
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _validate_routes(routes: Mapping[str, str]) -> dict[str, str]:
    normalized = {str(role).casefold(): str(provider).casefold() for role, provider in routes.items()}
    if set(normalized) != set(ROLE_NAMES):
        raise ValidationError("Council routes must contain exactly CTO, CPO, and CMO")
    if any(provider not in {"codex", "claude"} for provider in normalized.values()):
        raise ValidationError("Council providers must be codex or claude")
    if "claude" not in normalized.values():
        raise ValidationError("At least one executive role must use Claude")
    return {role: normalized[role] for role in ROLE_NAMES}


def _validate_response(response: Any, role: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValidationError("Executive response must be an object")
    required = {
        "schema_version",
        "role",
        "speech",
        "contribution",
        "questions",
        "advisory",
        "evidence_refs",
        "unresolved_decisions",
    }
    if set(response) != required:
        raise ValidationError("Executive response fields do not match schema version 1")
    if response["schema_version"] != 1 or str(response["role"]).casefold() != role:
        raise ValidationError("Executive response role or schema version is invalid")
    if not isinstance(response["speech"], str) or not response["speech"].strip():
        raise ValidationError("Executive speech must not be empty")
    if not isinstance(response["contribution"], dict):
        raise ValidationError("Executive contribution must be an object")
    for name in ("questions", "advisory", "evidence_refs", "unresolved_decisions"):
        if not isinstance(response[name], list) or not all(
            isinstance(item, (str, dict)) for item in response[name]
        ):
            raise ValidationError(f"Executive {name} must be a list")
    return response


class InteractiveCouncil:
    """Coordinate exactly three durable, provider-separated executive voices."""

    def __init__(
        self,
        company: CompanyOS,
        *,
        runner: ExecutiveRunner,
        role_routes: Mapping[str, str] | None = None,
        time_limit_seconds: int = 600,
    ) -> None:
        self.company = company
        self.runner = runner
        self.role_routes = _validate_routes(role_routes or DEFAULT_ROLE_ROUTES)
        if time_limit_seconds <= 0 or time_limit_seconds > 1200:
            raise ValidationError("Council role time limit must be between 1 and 1200 seconds")
        self.time_limit_seconds = int(time_limit_seconds)

    def open(self, idea_id: str, *, idempotency_key: str) -> dict[str, Any]:
        self.company.idea(idea_id)
        payload = {"idea_id": idea_id, "role_routes": self.role_routes}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            session_id = new_id("council_session")
            now = utc_now()
            self.company.store.insert_row(
                "council_sessions",
                {
                    "id": session_id,
                    "idea_id": idea_id,
                    "status": "OPEN",
                    "role_routes_json": canonical_json(self.role_routes),
                    "created_at": now,
                    "updated_at": now,
                },
                connection=connection,
            )
            self.company.store.append_event(
                "COUNCIL_SESSION_OPENED",
                aggregate_type="CouncilSession",
                aggregate_id=session_id,
                payload=payload,
                connection=connection,
            )
            return {"session_id": session_id, "idea_id": idea_id, "status": "OPEN"}

        try:
            return self.company.store.run_idempotent(
                idempotency_key, "open_interactive_council", payload, operation
            )
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc

    def turn(
        self,
        session_id: str,
        *,
        message_file: str | Path,
        idempotency_key: str,
    ) -> dict[str, Any]:
        message = self._read_message(message_file)
        message_sha = sha256(message.encode("utf-8")).hexdigest()
        command_payload = {"session_id": session_id, "message_sha256": message_sha}
        created_paths: list[Path] = []

        try:
            with self.company.store.transaction() as connection:
                self._assert_running(connection)
                claim = self.company.store.claim_idempotency(
                    idempotency_key,
                    "interactive_council_turn",
                    command_payload,
                    connection=connection,
                )
                if not claim.is_new:
                    if claim.completed and isinstance(claim.result, dict):
                        return dict(claim.result)
                    raise ConflictError("Council turn with this key is still in progress")
                session = connection.execute(
                    "SELECT * FROM council_sessions WHERE id=?", (session_id,)
                ).fetchone()
                if session is None:
                    raise NotFoundError(f"Council session not found: {session_id}")
                if session["status"] != "OPEN":
                    raise ValidationError("Council session is closed")
                turn_number = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(turn_number), 0) + 1 FROM council_turns WHERE session_id=?",
                        (session_id,),
                    ).fetchone()[0]
                )
                turn_id = new_id("council_turn")
                frozen = self._frozen_input(connection, session, turn_number, message)
                frozen_json = canonical_json(frozen)
                base = contained_path(
                    self.company.root,
                    "var",
                    "council-room",
                    session_id,
                    f"turn-{turn_number:04d}",
                )
                message_path = base / "ceo-message.txt"
                frozen_path = base / "frozen-input.json"
                atomic_write_text(message_path, message + "\n")
                atomic_write_text(frozen_path, frozen_json + "\n")
                created_paths.extend((message_path, frozen_path))
                now = utc_now()
                self.company.store.insert_row(
                    "council_turns",
                    {
                        "id": turn_id,
                        "session_id": session_id,
                        "turn_number": turn_number,
                        "status": "RUNNING",
                        "ceo_message_path": self.company._relative(message_path),
                        "ceo_message_sha256": sha256_file(message_path),
                        "frozen_input_path": self.company._relative(frozen_path),
                        "frozen_input_sha256": sha256(frozen_json.encode("utf-8")).hexdigest(),
                        "created_at": now,
                        "updated_at": now,
                    },
                    connection=connection,
                )
                evidence_id = new_id("evidence")
                self.company.store.insert_row(
                    "evidence",
                    {
                        "id": evidence_id,
                        "idea_id": session["idea_id"],
                        "venture_id": None,
                        "work_order_id": None,
                        "run_id": None,
                        "external_ref": f"ceo-statement:{session_id}:{turn_number}",
                        "kind": "CEO_STATEMENT",
                        "path": self.company._relative(message_path),
                        "sha256": sha256_file(message_path),
                        "trusted": 0,
                        "payload_json": canonical_json(
                            {
                                "schema_version": 1,
                                "session_id": session_id,
                                "turn_id": turn_id,
                                "turn_number": turn_number,
                                "claimed_actor": "CEO",
                                "supports_requirements": True,
                                "supports_scope_decisions": True,
                                "supports_external_fact": False,
                                "supports_validated_market_claim": False,
                            }
                        ),
                        "created_at": now,
                    },
                    connection=connection,
                )
                self.company.store.append_event(
                    "CEO_STATEMENT_RECORDED",
                    aggregate_type="CouncilTurn",
                    aggregate_id=turn_id,
                    correlation_id=session_id,
                    payload={
                        "evidence_id": evidence_id,
                        "message_sha256": sha256_file(message_path),
                        "turn_number": turn_number,
                        "supports_external_fact": False,
                    },
                    connection=connection,
                )
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc
        except BaseException:
            for path in reversed(created_paths):
                path.unlink(missing_ok=True)
            raise

        for role in ROLE_NAMES:
            self._execute_role(turn_id, role)
        result = self._turn_status(turn_id)
        with self.company.store.transaction() as connection:
            self.company.store.complete_idempotency(
                idempotency_key,
                result,
                command="interactive_council_turn",
                connection=connection,
            )
        return result

    def retry(
        self,
        session_id: str,
        *,
        turn_number: int,
        role: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        normalized_role = role.casefold()
        if normalized_role not in ROLE_NAMES:
            raise ValidationError("Council retry role must be CTO, CPO, or CMO")
        turn = self.company.store.query_one(
            "SELECT * FROM council_turns WHERE session_id=? AND turn_number=?",
            (session_id, int(turn_number)),
        )
        if turn is None:
            raise NotFoundError(f"Council turn not found: {session_id}/{turn_number}")
        latest = self._latest_role_run(turn["id"], normalized_role)
        if latest is None or latest["status"] not in RETRYABLE_STATUSES:
            raise ConflictError("Only an unfinished executive role can be retried")
        payload = {
            "session_id": session_id,
            "turn_number": int(turn_number),
            "role": normalized_role,
            "prior_run_id": latest["id"],
        }
        try:
            with self.company.store.transaction() as connection:
                claim = self.company.store.claim_idempotency(
                    idempotency_key,
                    "retry_interactive_council_role",
                    payload,
                    connection=connection,
                )
                if not claim.is_new:
                    if claim.completed and isinstance(claim.result, dict):
                        return dict(claim.result)
                    raise ConflictError("Council retry with this key is still in progress")
            self._execute_role(turn["id"], normalized_role, retry=True)
            result = self._turn_status(turn["id"])
            with self.company.store.transaction() as connection:
                self.company.store.complete_idempotency(
                    idempotency_key,
                    result,
                    command="retry_interactive_council_role",
                    connection=connection,
                )
            return result
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc

    def status(self, session_id: str) -> dict[str, Any]:
        session = self.company.store.query_one(
            "SELECT * FROM council_sessions WHERE id=?", (session_id,)
        )
        if session is None:
            raise NotFoundError(f"Council session not found: {session_id}")
        turns = self.company.store.query_all(
            "SELECT * FROM council_turns WHERE session_id=? ORDER BY turn_number",
            (session_id,),
        )
        return {
            "session_id": session_id,
            "idea_id": session["idea_id"],
            "status": session["status"],
            "role_routes": json.loads(session["role_routes_json"]),
            "turn_count": len(turns),
            "turns": [self._turn_status(turn["id"]) for turn in turns],
        }

    def close(self, session_id: str, *, idempotency_key: str) -> dict[str, Any]:
        payload = {"session_id": session_id}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            session = connection.execute(
                "SELECT * FROM council_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session is None:
                raise NotFoundError(f"Council session not found: {session_id}")
            unfinished = connection.execute(
                "SELECT COUNT(*) FROM council_turns WHERE session_id=? AND status!='COMPLETED'",
                (session_id,),
            ).fetchone()[0]
            if unfinished:
                raise ValidationError("Council session has unfinished executive responses")
            now = utc_now()
            connection.execute(
                "UPDATE council_sessions SET status='CLOSED', updated_at=? WHERE id=?",
                (now, session_id),
            )
            self.company.store.append_event(
                "COUNCIL_SESSION_CLOSED",
                aggregate_type="CouncilSession",
                aggregate_id=session_id,
                payload={"turn_count": connection.execute(
                    "SELECT COUNT(*) FROM council_turns WHERE session_id=?", (session_id,)
                ).fetchone()[0]},
                connection=connection,
            )
            return {"session_id": session_id, "status": "CLOSED"}

        try:
            return self.company.store.run_idempotent(
                idempotency_key, "close_interactive_council", payload, operation
            )
        except IdempotencyConflict as exc:
            raise ConflictError(str(exc)) from exc

    def assert_external_fact_supported(self, evidence_id: str) -> None:
        evidence = self.company.store.get_row("evidence", evidence_id)
        if evidence is None:
            raise NotFoundError(f"Evidence not found: {evidence_id}")
        payload = json.loads(evidence["payload_json"])
        if not bool(evidence["trusted"]) or not payload.get("supports_external_fact"):
            raise ValidationError("Evidence cannot support an external fact claim")

    def _execute_role(self, turn_id: str, role: str, *, retry: bool = False) -> None:
        with self.company.store.transaction() as connection:
            self._assert_running(connection)
            turn = connection.execute(
                "SELECT * FROM council_turns WHERE id=?", (turn_id,)
            ).fetchone()
            if turn is None:
                raise NotFoundError(f"Council turn not found: {turn_id}")
            session = connection.execute(
                "SELECT * FROM council_sessions WHERE id=?", (turn["session_id"],)
            ).fetchone()
            assert session is not None
            latest = connection.execute(
                "SELECT * FROM council_role_runs WHERE turn_id=? AND role=? ORDER BY attempt DESC LIMIT 1",
                (turn_id, role),
            ).fetchone()
            if latest is not None:
                if latest["status"] == "COMPLETED":
                    if retry:
                        raise ConflictError("Completed executive role cannot be retried")
                    return
                if latest["status"] == "EXECUTING":
                    if datetime.now(timezone.utc) <= datetime.fromisoformat(latest["lease_expires_at"]):
                        raise ConflictError("Executive role is already executing")
                    connection.execute(
                        "UPDATE council_role_runs SET status='EXPIRED', updated_at=? WHERE id=?",
                        (utc_now(), latest["id"]),
                    )
                elif not retry:
                    return
            attempt = 1 if latest is None else int(latest["attempt"]) + 1
            fence_token = 1 if latest is None else int(latest["fence_token"]) + 1
            execution_id = new_id("council_execution")
            run_id = new_id("council_role_run")
            lease_expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=self.time_limit_seconds)
            ).isoformat()
            now = utc_now()
            self.company.store.insert_row(
                "council_role_runs",
                {
                    "id": run_id,
                    "turn_id": turn_id,
                    "role": role,
                    "provider": self.role_routes[role],
                    "status": "EXECUTING",
                    "attempt": attempt,
                    "execution_id": execution_id,
                    "fence_token": fence_token,
                    "lease_expires_at": lease_expires_at,
                    "input_sha256": turn["frozen_input_sha256"],
                    "output_path": None,
                    "output_sha256": None,
                    "model": None,
                    "usage_json": "{}",
                    "duration_seconds": None,
                    "error": None,
                    "created_at": now,
                    "updated_at": now,
                },
                connection=connection,
            )
            self.company.store.append_event(
                "COUNCIL_ROLE_EXECUTION_STARTED",
                aggregate_type="CouncilRoleRun",
                aggregate_id=run_id,
                correlation_id=turn["session_id"],
                payload={
                    "turn_id": turn_id,
                    "turn_number": int(turn["turn_number"]),
                    "role": role,
                    "provider": self.role_routes[role],
                    "attempt": attempt,
                    "execution_id": execution_id,
                    "fence_token": fence_token,
                    "lease_expires_at": lease_expires_at,
                },
                connection=connection,
            )

        frozen_path = self.company._absolute(turn["frozen_input_path"])
        frozen_json = frozen_path.read_text(encoding="utf-8").strip()
        workspace = contained_path(
            self.company.root,
            "var",
            "council-room",
            turn["session_id"],
            "roles",
            role,
        )
        workspace.mkdir(parents=True, exist_ok=True)
        request = ExecutiveRequest(
            session_id=turn["session_id"],
            turn_id=turn_id,
            turn_number=int(turn["turn_number"]),
            role=role,
            provider=self.role_routes[role],
            prompt=self._prompt(role, frozen_json),
            workspace=workspace,
            frozen_input_json=frozen_json,
            frozen_input_sha256=turn["frozen_input_sha256"],
            time_limit_seconds=self.time_limit_seconds,
        )
        try:
            outcome = self.runner.run(request)
            if not isinstance(outcome, ExecutiveOutcome):
                raise TypeError("Executive runner must return ExecutiveOutcome")
        except BaseException as exc:
            outcome = ExecutiveOutcome(
                status="CLI_FAILED",
                response=None,
                provider=self.role_routes[role],
                model=None,
                duration_seconds=0.0,
                error=f"{type(exc).__name__}: {exc}",
            )

        status = outcome.status
        response: dict[str, Any] | None = None
        if outcome.provider != self.role_routes[role]:
            status = "INVALID_RESPONSE"
            error = "Executive outcome provider does not match the claimed route"
        else:
            error = outcome.error
        if status == "COMPLETED":
            try:
                response = _validate_response(outcome.response, role)
            except ValidationError as exc:
                status = "INVALID_RESPONSE"
                error = str(exc)
        elif status not in RETRYABLE_STATUSES:
            status = "CLI_FAILED"
            error = error or f"unsupported executive outcome status: {outcome.status}"

        output_path: Path | None = None
        output_sha: str | None = None
        if response is not None:
            output_path = contained_path(
                self.company.root,
                "var",
                "council-room",
                turn["session_id"],
                f"turn-{int(turn['turn_number']):04d}",
                f"{role}-attempt-{attempt}.json",
            )
            atomic_write_json(output_path, response)
            output_sha = sha256_file(output_path)

        stale = False
        with self.company.store.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM council_role_runs WHERE id=?", (run_id,)
            ).fetchone()
            latest = connection.execute(
                "SELECT * FROM council_role_runs WHERE turn_id=? AND role=? ORDER BY attempt DESC LIMIT 1",
                (turn_id, role),
            ).fetchone()
            if (
                current is None
                or latest is None
                or latest["id"] != run_id
                or current["status"] != "EXECUTING"
                or current["execution_id"] != execution_id
                or int(current["fence_token"]) != fence_token
            ):
                stale = True
                self.company.store.append_event(
                    "STALE_COUNCIL_RESULT_REJECTED",
                    aggregate_type="CouncilRoleRun",
                    aggregate_id=run_id,
                    correlation_id=turn["session_id"],
                    payload={"turn_id": turn_id, "role": role, "execution_id": execution_id},
                    connection=connection,
                )
            else:
                if datetime.now(timezone.utc) > datetime.fromisoformat(lease_expires_at):
                    status = "EXPIRED"
                    response = None
                    error = "executive result arrived after lease expiry"
                    output_path = None
                    output_sha = None
                connection.execute(
                    """
                    UPDATE council_role_runs
                    SET status=?, output_path=?, output_sha256=?, model=?, usage_json=?,
                        duration_seconds=?, error=?, updated_at=?
                    WHERE id=? AND status='EXECUTING' AND execution_id=? AND fence_token=?
                    """,
                    (
                        status,
                        None if output_path is None else self.company._relative(output_path),
                        output_sha,
                        outcome.model,
                        canonical_json(outcome.usage),
                        float(outcome.duration_seconds),
                        error,
                        utc_now(),
                        run_id,
                        execution_id,
                        fence_token,
                    ),
                )
                self.company.store.append_event(
                    "COUNCIL_ROLE_EXECUTION_FINALIZED",
                    aggregate_type="CouncilRoleRun",
                    aggregate_id=run_id,
                    correlation_id=turn["session_id"],
                    payload={
                        "turn_id": turn_id,
                        "role": role,
                        "provider": outcome.provider,
                        "model": outcome.model,
                        "status": status,
                        "attempt": attempt,
                        "execution_id": execution_id,
                        "fence_token": fence_token,
                        "output_sha256": output_sha,
                        "usage": outcome.usage,
                    },
                    connection=connection,
                )
                self._update_turn_status(connection, turn_id)
        if stale and output_path is not None:
            output_path.unlink(missing_ok=True)

    def _turn_status(self, turn_id: str) -> dict[str, Any]:
        turn = self.company.store.query_one(
            "SELECT * FROM council_turns WHERE id=?", (turn_id,)
        )
        if turn is None:
            raise NotFoundError(f"Council turn not found: {turn_id}")
        roles: dict[str, Any] = {}
        for role in ROLE_NAMES:
            run = self._latest_role_run(turn_id, role)
            if run is None:
                roles[role] = {"status": "PENDING", "provider": self.role_routes[role]}
                continue
            response = None
            if run["output_path"]:
                response = json.loads(
                    self.company._absolute(run["output_path"]).read_text(encoding="utf-8")
                )
            roles[role] = {
                "run_id": run["id"],
                "status": run["status"],
                "provider": run["provider"],
                "model": run["model"],
                "attempt": int(run["attempt"]),
                "usage": json.loads(run["usage_json"]),
                "output_sha256": run["output_sha256"],
                "response": response,
                "error": run["error"],
            }
        return {
            "turn_id": turn_id,
            "session_id": turn["session_id"],
            "turn_number": int(turn["turn_number"]),
            "status": turn["status"],
            "ceo_message": self.company._absolute(turn["ceo_message_path"])
            .read_text(encoding="utf-8")
            .strip(),
            "ceo_message_sha256": turn["ceo_message_sha256"],
            "frozen_input_sha256": turn["frozen_input_sha256"],
            "roles": roles,
        }

    def _latest_role_run(self, turn_id: str, role: str) -> sqlite3.Row | None:
        return self.company.store.query_one(
            "SELECT * FROM council_role_runs WHERE turn_id=? AND role=? ORDER BY attempt DESC LIMIT 1",
            (turn_id, role),
        )

    @staticmethod
    def _read_message(path: str | Path) -> str:
        source = Path(path)
        if not source.is_file():
            raise ValidationError(f"CEO message file not found: {source}")
        content = source.read_bytes()
        if len(content) > _MAX_MESSAGE_BYTES:
            raise ValidationError("CEO message is too large")
        try:
            text = content.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ValidationError("CEO message must be UTF-8") from exc
        if not text:
            raise ValidationError("CEO message must not be empty")
        return text

    def _frozen_input(
        self,
        connection: sqlite3.Connection,
        session: sqlite3.Row,
        turn_number: int,
        message: str,
    ) -> dict[str, Any]:
        prior_turns = []
        rows = connection.execute(
            "SELECT * FROM council_turns WHERE session_id=? ORDER BY turn_number",
            (session["id"],),
        ).fetchall()
        for row in rows:
            outputs: dict[str, Any] = {}
            for role in ROLE_NAMES:
                run = connection.execute(
                    "SELECT * FROM council_role_runs WHERE turn_id=? AND role=? AND status='COMPLETED' ORDER BY attempt DESC LIMIT 1",
                    (row["id"], role),
                ).fetchone()
                if run is not None and run["output_path"]:
                    outputs[role] = json.loads(
                        self.company._absolute(run["output_path"]).read_text(encoding="utf-8")
                    )
            prior_turns.append(
                {
                    "turn_number": int(row["turn_number"]),
                    "ceo_message": self.company._absolute(row["ceo_message_path"])
                    .read_text(encoding="utf-8")
                    .strip(),
                    "role_outputs": outputs,
                }
            )
        idea = connection.execute(
            "SELECT id, text FROM ideas WHERE id=?", (session["idea_id"],)
        ).fetchone()
        assert idea is not None
        return {
            "schema_version": 1,
            "session_id": session["id"],
            "idea": {"id": idea["id"], "text": idea["text"]},
            "turn_number": turn_number,
            "ceo_message": message,
            "prior_turns": prior_turns,
            "sharing_rule": "CURRENT_TURN_ROLE_OUTPUTS_ARE_SHARED_NEXT_TURN_ONLY",
        }

    @staticmethod
    def _update_turn_status(connection: sqlite3.Connection, turn_id: str) -> None:
        latest = connection.execute(
            """
            SELECT r.role, r.status
            FROM council_role_runs r
            JOIN (
                SELECT role, MAX(attempt) AS attempt
                FROM council_role_runs WHERE turn_id=? GROUP BY role
            ) latest ON latest.role=r.role AND latest.attempt=r.attempt
            WHERE r.turn_id=?
            """,
            (turn_id, turn_id),
        ).fetchall()
        statuses = {row["role"]: row["status"] for row in latest}
        if all(statuses.get(role) == "COMPLETED" for role in ROLE_NAMES):
            status = "COMPLETED"
        elif any(statuses.get(role) == "EXECUTING" for role in ROLE_NAMES):
            status = "RUNNING"
        else:
            status = "PARTIAL"
        connection.execute(
            "UPDATE council_turns SET status=?, updated_at=? WHERE id=?",
            (status, utc_now(), turn_id),
        )

    @staticmethod
    def _assert_running(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT value_json FROM global_state WHERE key='stopped'"
        ).fetchone()
        if row is not None and bool(json.loads(row["value_json"])):
            raise CompanyStoppedError("Company execution is stopped; run company resume")

    @staticmethod
    def _prompt(role: str, frozen_input_json: str) -> str:
        role_spec = serialize_role_spec(role)
        return (
            "You are one executive in a three-person product council. "
            "Do not invoke tools, other agents, company commands, or council commands. "
            "Use only the frozen shared input and your role specification. "
            "Return exactly the requested JSON object.\n\n"
            f"ROLE_SPEC={canonical_json(role_spec)}\n"
            f"FROZEN_INPUT={frozen_input_json}"
        )


__all__ = [
    "DEFAULT_ROLE_ROUTES",
    "ExecutiveOutcome",
    "ExecutiveRequest",
    "ExecutiveRunner",
    "InteractiveCouncil",
    "ROLE_NAMES",
    "SubscriptionExecutiveRunner",
]
