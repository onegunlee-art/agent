from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
import subprocess

import pytest

import company_os.council_room as council_room_module
from company_os.application import CompanyOS
from company_os.council_room import (
    ExecutiveOutcome,
    ExecutiveRequest,
    InteractiveCouncil,
    SubscriptionExecutiveRunner,
)
from company_os.errors import CompanyStoppedError, ConflictError, ValidationError


ROLES = ("cto", "cpo", "cmo")


def response_for(role: str, turn: int) -> dict:
    return {
        "schema_version": 1,
        "role": role,
        "speech": f"{role.upper()} public response for turn {turn}",
        "contribution": {"turn": turn, "owner": role},
        "questions": [f"{role} question {turn}"],
        "advisory": [],
        "evidence_refs": [],
        "unresolved_decisions": [f"decision-{role}-{turn}"],
    }


@dataclass
class RecordingRunner:
    company: CompanyOS
    fail_once: set[tuple[int, str]] = field(default_factory=set)
    calls: list[ExecutiveRequest] = field(default_factory=list)

    def run(self, request: ExecutiveRequest) -> ExecutiveOutcome:
        assert self.company.store.connection.in_transaction is False
        self.calls.append(request)
        key = (request.turn_number, request.role)
        if key in self.fail_once:
            self.fail_once.remove(key)
            return ExecutiveOutcome(
                status="QUOTA_WAIT",
                response=None,
                provider=request.provider,
                model=None,
                usage={},
                duration_seconds=0.01,
                error="subscription quota temporarily unavailable",
            )
        return ExecutiveOutcome(
            status="COMPLETED",
            response=response_for(request.role, request.turn_number),
            provider=request.provider,
            model=f"model-{request.provider}",
            usage={"input_tokens": 10, "output_tokens": 5},
            duration_seconds=0.02,
        )


@pytest.fixture
def council(tmp_path: Path):
    company = CompanyOS(tmp_path, db_path=tmp_path / "ledger.sqlite3").initialize()
    idea = company.create_idea(
        "Build a synthetic local product through an executive conversation.",
        idempotency_key="idea-v09",
    )
    runner = RecordingRunner(company)
    room = InteractiveCouncil(company, runner=runner)
    try:
        yield company, idea, room, runner
    finally:
        company.close()


def write_message(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def write_decisions(path: Path, session_id: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": session_id,
                "decisions": [
                    {
                        "decision_id": "scope-v1",
                        "decision": "합성 로컬 메모 제품만 개발한다.",
                        "rationale": "CEO가 첫 제품 범위를 명시적으로 선택했다.",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return path


def test_two_turns_route_exact_roles_and_freeze_shared_input(council, tmp_path):
    company, idea, room, runner = council
    opened = room.open(idea.id, idempotency_key="open-1")

    first = room.turn(
        opened["session_id"],
        message_file=write_message(tmp_path / "turn1.txt", "데스크톱에서 쓰고 싶다."),
        idempotency_key="turn-1",
    )
    second = room.turn(
        opened["session_id"],
        message_file=write_message(tmp_path / "turn2.txt", "오프라인 사용을 우선한다."),
        idempotency_key="turn-2",
    )

    assert first["status"] == second["status"] == "COMPLETED"
    assert [(call.turn_number, call.role, call.provider) for call in runner.calls] == [
        (1, "cto", "codex"),
        (1, "cpo", "claude"),
        (1, "cmo", "codex"),
        (2, "cto", "codex"),
        (2, "cpo", "claude"),
        (2, "cmo", "codex"),
    ]
    for turn_number in (1, 2):
        inputs = [call for call in runner.calls if call.turn_number == turn_number]
        assert len({call.frozen_input_sha256 for call in inputs}) == 1
        assert len({call.frozen_input_json for call in inputs}) == 1
    turn_two_input = json.loads(runner.calls[3].frozen_input_json)
    assert set(turn_two_input["prior_turns"][0]["role_outputs"]) == set(ROLES)
    assert "turn 2" not in runner.calls[3].frozen_input_json

    reopened = InteractiveCouncil(company, runner=runner).status(opened["session_id"])
    assert reopened["turn_count"] == 2
    assert reopened["turns"][1]["ceo_message"] == "오프라인 사용을 우선한다."


def test_retry_preserves_completed_roles_and_does_not_duplicate_ceo_statement(
    council, tmp_path
):
    company, idea, room, runner = council
    runner.fail_once.add((1, "cpo"))
    session_id = room.open(idea.id, idempotency_key="open-2")["session_id"]
    message = write_message(tmp_path / "message.txt", "간단한 메모 도구를 원한다.")

    partial = room.turn(session_id, message_file=message, idempotency_key="turn-partial")
    assert partial["status"] == "PARTIAL"
    assert partial["roles"]["cpo"]["status"] == "QUOTA_WAIT"
    assert partial["roles"]["cto"]["status"] == "COMPLETED"
    assert partial["roles"]["cmo"]["status"] == "COMPLETED"

    retried = room.retry(
        session_id,
        turn_number=1,
        role="cpo",
        idempotency_key="retry-cpo-new-key",
    )
    assert retried["status"] == "COMPLETED"
    assert [(call.turn_number, call.role) for call in runner.calls] == [
        (1, "cto"),
        (1, "cpo"),
        (1, "cmo"),
        (1, "cpo"),
    ]
    assert company.store.scalar("SELECT COUNT(*) FROM council_turns") == 1
    assert company.store.scalar(
        "SELECT COUNT(*) FROM evidence WHERE kind='CEO_STATEMENT'"
    ) == 1

    replay = room.turn(session_id, message_file=message, idempotency_key="turn-partial")
    assert replay["turn_id"] == partial["turn_id"]
    assert len(runner.calls) == 4
    with pytest.raises(ConflictError):
        room.retry(
            session_id,
            turn_number=1,
            role="cto",
            idempotency_key="retry-completed-role",
        )


def test_retry_reclaims_an_interrupted_expired_role_attempt(council, tmp_path):
    company, idea, room, runner = council
    runner.fail_once.add((1, "cpo"))
    session_id = room.open(idea.id, idempotency_key="open-expired")["session_id"]
    room.turn(
        session_id,
        message_file=write_message(tmp_path / "expired.txt", "중단 복구를 시험한다."),
        idempotency_key="turn-expired",
    )
    with company.store.transaction() as connection:
        row = connection.execute(
            "SELECT id FROM council_role_runs WHERE role='cpo' ORDER BY attempt DESC LIMIT 1"
        ).fetchone()
        connection.execute(
            "UPDATE council_role_runs SET status='EXECUTING', lease_expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", row["id"]),
        )

    result = room.retry(
        session_id,
        turn_number=1,
        role="cpo",
        idempotency_key="retry-expired",
    )

    assert result["status"] == "COMPLETED"
    attempts = company.store.query_all(
        "SELECT status FROM council_role_runs WHERE role='cpo' ORDER BY attempt"
    )
    assert [row["status"] for row in attempts] == ["EXPIRED", "COMPLETED"]


def test_stopped_turn_resumes_roles_that_never_started_without_duplicate_statement(
    council, tmp_path
):
    company, idea, room, runner = council
    stopped_once = False
    normal_run = runner.run

    def stop_after_cto(request):
        nonlocal stopped_once
        outcome = normal_run(request)
        if request.role == "cto" and not stopped_once:
            stopped_once = True
            company.stop()
        return outcome

    runner.run = stop_after_cto
    session_id = room.open(idea.id, idempotency_key="open-stop-resume")["session_id"]
    message = write_message(tmp_path / "stop.txt", "중단 뒤 같은 턴을 이어간다.")

    with pytest.raises(CompanyStoppedError):
        room.turn(session_id, message_file=message, idempotency_key="turn-stop-resume")
    partial = room.status(session_id)["turns"][0]
    assert partial["roles"]["cto"]["status"] == "COMPLETED"
    assert partial["roles"]["cpo"]["status"] == "PENDING"

    company.resume()
    room.retry(
        session_id, turn_number=1, role="cpo", idempotency_key="resume-cpo"
    )
    completed = room.retry(
        session_id, turn_number=1, role="cmo", idempotency_key="resume-cmo"
    )

    assert completed["status"] == "COMPLETED"
    assert company.store.scalar(
        "SELECT COUNT(*) FROM evidence WHERE kind='CEO_STATEMENT'"
    ) == 1
    replay = room.turn(
        session_id, message_file=message, idempotency_key="turn-stop-resume"
    )
    assert replay["turn_id"] == completed["turn_id"]


def test_crash_before_first_role_claim_resumes_same_turn(council, tmp_path, monkeypatch):
    company, idea, room, _runner = council
    session_id = room.open(idea.id, idempotency_key="open-preclaim-crash")["session_id"]
    message = write_message(tmp_path / "crash.txt", "첫 역할 청구 전 중단을 복구한다.")
    execute_role = room._execute_role
    crashed = False

    def crash_once(turn_id, role, *, retry=False):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise KeyboardInterrupt()
        return execute_role(turn_id, role, retry=retry)

    monkeypatch.setattr(room, "_execute_role", crash_once)
    with pytest.raises(KeyboardInterrupt):
        room.turn(session_id, message_file=message, idempotency_key="turn-preclaim")
    monkeypatch.setattr(room, "_execute_role", execute_role)

    for role in ROLES:
        result = room.retry(
            session_id,
            turn_number=1,
            role=role,
            idempotency_key=f"resume-{role}",
        )
    assert result["status"] == "COMPLETED"
    assert company.store.scalar("SELECT COUNT(*) FROM council_turns") == 1
    assert company.store.scalar(
        "SELECT COUNT(*) FROM evidence WHERE kind='CEO_STATEMENT'"
    ) == 1


def test_new_turn_rejects_unfinished_prior_turn(council, tmp_path):
    _company, idea, room, runner = council
    runner.fail_once.add((1, "cpo"))
    session_id = room.open(idea.id, idempotency_key="open-prior-partial")["session_id"]
    room.turn(
        session_id,
        message_file=write_message(tmp_path / "partial.txt", "첫 턴이다."),
        idempotency_key="partial-turn",
    )

    with pytest.raises(ValidationError, match="unfinished prior turn"):
        room.turn(
            session_id,
            message_file=write_message(tmp_path / "too-soon.txt", "다음 턴이다."),
            idempotency_key="too-soon-turn",
        )


def test_prior_message_and_role_output_hashes_are_checked_before_reuse(council, tmp_path):
    company, idea, room, _runner = council
    session_id = room.open(idea.id, idempotency_key="open-tamper-history")["session_id"]
    room.turn(
        session_id,
        message_file=write_message(tmp_path / "history.txt", "보존할 첫 메시지다."),
        idempotency_key="history-turn",
    )
    turn = company.store.query_one("SELECT * FROM council_turns WHERE session_id=?", (session_id,))
    company._absolute(turn["ceo_message_path"]).write_text("tampered", encoding="utf-8")

    with pytest.raises(ValidationError, match="hash binding"):
        room.status(session_id)
    with pytest.raises(ValidationError, match="hash binding"):
        room.turn(
            session_id,
            message_file=write_message(tmp_path / "second.txt", "두 번째 메시지다."),
            idempotency_key="second-after-tamper",
        )


def test_current_frozen_input_is_verified_before_retry_claim_or_provider_call(
    council, tmp_path
):
    company, idea, room, runner = council
    runner.fail_once.add((1, "cpo"))
    session_id = room.open(idea.id, idempotency_key="open-current-input-tamper")[
        "session_id"
    ]
    room.turn(
        session_id,
        message_file=write_message(tmp_path / "original.txt", "원래 CEO 메시지다."),
        idempotency_key="turn-current-input-tamper",
    )
    turn = company.store.query_one(
        "SELECT * FROM council_turns WHERE session_id=?", (session_id,)
    )
    frozen_path = company._absolute(turn["frozen_input_path"])
    tampered = json.loads(frozen_path.read_text(encoding="utf-8"))
    tampered["ceo_message"] = "변조된 CEO 메시지다."
    frozen_path.write_text(
        json.dumps(tampered, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    call_count = len(runner.calls)
    run_count = company.store.scalar(
        "SELECT COUNT(*) FROM council_role_runs WHERE turn_id=?", (turn["id"],)
    )
    event_count = company.store.scalar(
        "SELECT COUNT(*) FROM events WHERE aggregate_type='CouncilRoleRun'"
    )

    with pytest.raises(ValidationError, match="Frozen council input failed its hash binding"):
        room.retry(
            session_id,
            turn_number=1,
            role="cpo",
            idempotency_key="retry-current-input-tamper",
        )

    assert len(runner.calls) == call_count
    assert company.store.scalar(
        "SELECT COUNT(*) FROM council_role_runs WHERE turn_id=?", (turn["id"],)
    ) == run_count
    assert company.store.scalar(
        "SELECT COUNT(*) FROM events WHERE aggregate_type='CouncilRoleRun'"
    ) == event_count
    completed = company.store.query_all(
        "SELECT role FROM council_role_runs WHERE turn_id=? AND status='COMPLETED'",
        (turn["id"],),
    )
    assert {row["role"] for row in completed} == {"cto", "cmo"}


def test_ceo_external_fact_is_preserved_but_cannot_be_trusted_fact(council, tmp_path):
    company, idea, room, _runner = council
    session_id = room.open(idea.id, idempotency_key="open-3")["session_id"]
    room.turn(
        session_id,
        message_file=write_message(
            tmp_path / "market.txt", "이 시장은 연 10조 원이라고 생각한다."
        ),
        idempotency_key="market-turn",
    )

    evidence = company.store.query_one(
        "SELECT * FROM evidence WHERE kind='CEO_STATEMENT'"
    )
    assert evidence is not None
    payload = json.loads(evidence["payload_json"])
    assert evidence["trusted"] == 0
    assert payload["supports_requirements"] is True
    assert payload["supports_external_fact"] is False
    with pytest.raises(ValidationError, match="external fact"):
        room.assert_external_fact_supported(evidence["id"])


def test_unresolved_executive_decisions_block_close_until_ceo_resolution_is_recorded(
    council, tmp_path
):
    company, idea, room, _runner = council
    session_id = room.open(idea.id, idempotency_key="open-resolution")["session_id"]
    room.turn(
        session_id,
        message_file=write_message(tmp_path / "scope.txt", "제품 범위를 논의하자."),
        idempotency_key="turn-resolution",
    )

    with pytest.raises(ValidationError, match="unresolved executive decisions"):
        room.close(session_id, idempotency_key="close-before-resolution")

    decision_file = write_decisions(tmp_path / "decisions.json", session_id)
    resolved = room.resolve_decisions(
        session_id,
        decision_file=decision_file,
        idempotency_key="resolve-decisions",
    )
    closed = room.close(session_id, idempotency_key="close-after-resolution")

    assert resolved["decision_count"] == 1
    assert closed["status"] == "CLOSED"
    evidence = company.store.query_one(
        "SELECT * FROM evidence WHERE kind='CEO_COUNCIL_DECISION'"
    )
    assert evidence is not None
    assert evidence["sha256"] == resolved["decision_sha256"]
    assert json.loads(evidence["payload_json"])["session_id"] == session_id
    event = company.store.query_one(
        "SELECT * FROM events WHERE event_type='COUNCIL_DECISIONS_RESOLVED'"
    )
    assert event is not None
    assert json.loads(event["payload_json"])["evidence_id"] == evidence["id"]


def test_council_decision_resolution_rejects_wrong_session(council, tmp_path):
    _company, idea, room, _runner = council
    session_id = room.open(idea.id, idempotency_key="open-wrong-resolution")[
        "session_id"
    ]
    decision_file = write_decisions(tmp_path / "wrong.json", "another-session")

    with pytest.raises(ValidationError, match="session_id"):
        room.resolve_decisions(
            session_id,
            decision_file=decision_file,
            idempotency_key="wrong-resolution",
        )


def test_turn_rejects_more_or_fewer_than_three_role_routes(council):
    company, idea, _room, runner = council
    with pytest.raises(ValidationError, match="exactly CTO, CPO, and CMO"):
        InteractiveCouncil(
            company,
            runner=runner,
            role_routes={"cto": "codex", "cpo": "claude"},
        )
    with pytest.raises(ValidationError, match="Claude"):
        InteractiveCouncil(
            company,
            runner=runner,
            role_routes={"cto": "codex", "cpo": "codex", "cmo": "codex"},
        )


def test_subscription_runner_uses_read_only_separate_cli_processes_and_safe_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    calls = []

    def process(command, *, cwd, environment, timeout, input_text):
        calls.append((command, cwd, environment, timeout, input_text))
        role = "cpo" if command[0].endswith("claude.exe") else "cto"
        response = response_for(role, 1)
        if role == "cto":
            schema_path = Path(command[command.index("--output-schema") + 1])
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            assert schema["properties"]["schema_version"]["type"] == "integer"
            assert schema["properties"]["role"]["type"] == "string"
            assert schema["properties"]["questions"]["maxItems"] == 6
            assert schema["properties"]["contribution"]["properties"]["details"]["maxItems"] == 12
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(response), encoding="utf-8")
            stdout = json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 11, "output_tokens": 7},
                }
            )
        else:
            stdout = json.dumps(
                {
                    "structured_output": response,
                    "model": "claude-current",
                    "usage": {"input_tokens": 13, "output_tokens": 6},
                }
            )
        import subprocess

        return subprocess.CompletedProcess(command, 0, stdout, "")

    runner = SubscriptionExecutiveRunner(
        codex_executable="codex.exe",
        claude_executable="C:/tools/claude.exe",
        codex_model="configured-astra",
        codex_effort="xhigh",
        process_runner=process,
    )
    long_prompt = "긴 프롬프트" * 5000
    base = dict(
        session_id="session-1",
        turn_id="turn-1",
        turn_number=1,
        prompt=long_prompt,
        frozen_input_json="{}",
        frozen_input_sha256="a" * 64,
        time_limit_seconds=60,
    )
    codex = runner.run(
        ExecutiveRequest(role="cto", provider="codex", workspace=tmp_path / "cto", **base)
    )
    claude = runner.run(
        ExecutiveRequest(role="cpo", provider="claude", workspace=tmp_path / "cpo", **base)
    )

    assert codex.status == claude.status == "COMPLETED"
    assert codex.model == "configured-astra"
    assert claude.model == "claude-current"
    assert "--sandbox" in calls[0][0] and "read-only" in calls[0][0]
    assert calls[0][0][calls[0][0].index("--model") + 1] == "configured-astra"
    assert "--no-session-persistence" in calls[1][0]
    assert long_prompt not in calls[1][0]
    assert calls[1][4] == long_prompt
    assert calls[0][1] != calls[1][1]
    for _command, _cwd, environment, _timeout, _input in calls:
        assert "OPENAI_API_KEY" not in environment
        assert "ANTHROPIC_API_KEY" not in environment


def test_process_runner_kills_child_tree_on_keyboard_interrupt(monkeypatch, tmp_path):
    class InterruptedProcess:
        pid = 4321
        returncode = None

        def __init__(self):
            self.calls = 0

        def communicate(self, *, input=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt()
            return "", ""

    process = InterruptedProcess()
    killed = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: killed.append(command)
        or subprocess.CompletedProcess(command, 0, "", ""),
    )
    if os.name != "nt":
        monkeypatch.setattr(
            council_room_module.os,
            "killpg",
            lambda pid, sig: killed.append((pid, sig)),
        )

    with pytest.raises(KeyboardInterrupt):
        SubscriptionExecutiveRunner._run_process(
            ["synthetic-cli"],
            cwd=tmp_path,
            environment={},
            timeout=10,
            input_text="prompt",
        )

    assert process.calls == 2
    if os.name == "nt":
        assert killed == [["taskkill", "/PID", "4321", "/T", "/F"]]
    else:
        assert killed == [(4321, signal.SIGKILL)]


def test_process_runner_kills_posix_process_group_on_keyboard_interrupt(
    monkeypatch, tmp_path
):
    class InterruptedProcess:
        pid = 8765
        returncode = None

        def __init__(self):
            self.calls = 0

        def communicate(self, *, input=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt()
            return "", ""

    class PosixOS:
        name = "posix"

        @staticmethod
        def killpg(pid, sig):
            killed.append((pid, sig))

    class PosixSignal:
        SIGKILL = getattr(signal, "SIGKILL", 9)

    process = InterruptedProcess()
    killed = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(council_room_module, "os", PosixOS)
    monkeypatch.setattr(council_room_module, "signal", PosixSignal)

    with pytest.raises(KeyboardInterrupt):
        SubscriptionExecutiveRunner._run_process(
            ["synthetic-cli"],
            cwd=tmp_path,
            environment={},
            timeout=10,
            input_text="prompt",
        )

    assert process.calls == 2
    assert killed == [(8765, PosixSignal.SIGKILL)]


def test_keyboard_interrupt_records_failed_role_and_does_not_start_next_role(
    council, tmp_path
):
    company, idea, room, runner = council

    def interrupt(_request):
        raise KeyboardInterrupt()

    runner.run = interrupt
    session_id = room.open(idea.id, idempotency_key="open-interrupt")["session_id"]
    with pytest.raises(KeyboardInterrupt):
        room.turn(
            session_id,
            message_file=write_message(tmp_path / "interrupt.txt", "실행을 중단한다."),
            idempotency_key="turn-interrupt",
        )

    runs = company.store.query_all("SELECT role, status FROM council_role_runs")
    assert [(row["role"], row["status"]) for row in runs] == [("cto", "CLI_FAILED")]


def test_default_codex_resolution_prefers_current_work_install_over_stale_path(
    tmp_path, monkeypatch
):
    executable = "codex.exe" if os.name == "nt" else "codex"
    current = tmp_path / ".codex" / ".sandbox-bin" / executable
    current.parent.mkdir(parents=True)
    current.write_bytes(b"synthetic executable")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "company_os.council_room.shutil.which", lambda _name: "C:/stale/codex.exe"
    )

    runner = SubscriptionExecutiveRunner()

    assert runner._resolve("codex", provider="codex") == str(current.resolve())
