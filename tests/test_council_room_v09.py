from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.council_room import (
    ExecutiveOutcome,
    ExecutiveRequest,
    InteractiveCouncil,
    SubscriptionExecutiveRunner,
)
from company_os.errors import ConflictError, ValidationError


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
        "Build a local product through an executive conversation.",
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
    base = dict(
        session_id="session-1",
        turn_id="turn-1",
        turn_number=1,
        prompt="bounded prompt",
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
    assert calls[0][1] != calls[1][1]
    for _command, _cwd, environment, _timeout, _input in calls:
        assert "OPENAI_API_KEY" not in environment
        assert "ANTHROPIC_API_KEY" not in environment


def test_default_codex_resolution_prefers_current_work_install_over_stale_path(
    tmp_path, monkeypatch
):
    current = tmp_path / ".codex" / ".sandbox-bin" / "codex.exe"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"synthetic executable")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "company_os.council_room.shutil.which", lambda _name: "C:/stale/codex.exe"
    )

    runner = SubscriptionExecutiveRunner()

    assert runner._resolve("codex", provider="codex") == str(current.resolve())
