from __future__ import annotations

import json
from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.council_room import ExecutiveOutcome, InteractiveCouncil
from company_os.dashboard import _render_page, read_dashboard
from company_os.errors import ValidationError
from company_os.product_factory import ProductFactory


class DeterministicExecutives:
    def run(self, request):
        return ExecutiveOutcome(
            status="COMPLETED",
            response={
                "schema_version": 1,
                "role": request.role,
                "speech": f"{request.role} turn {request.turn_number}",
                "contribution": {"role": request.role},
                "questions": [],
                "advisory": [],
                "evidence_refs": [],
                "unresolved_decisions": [],
            },
            provider=request.provider,
            model=f"fixture-{request.provider}",
        )


def write_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def valid_definition() -> dict:
    return {
        "schema_version": 1,
        "product_id": "synthetic-memo",
        "target": {
            "repository": "C:/dev/synthetic-memo",
            "remote_url": "https://github.com/example/synthetic-memo.git",
            "base_ref": "main",
            "delivery_ref": "main",
            "merge_to_main": True,
            "push": True,
            "server_deployment": False,
        },
        "product_brief": {
            "users": ["혼자 일하는 로컬 사용자"],
            "problem": "메모와 할 일을 한 파일에서 관리한다.",
            "features": ["추가", "목록", "완료"],
            "out_of_scope": ["로그인", "클라우드 동기화"],
            "constraints": ["Python 표준 라이브러리", "로컬 실행"],
            "completion": ["승인된 시나리오 수용시험 통과"],
        },
        "development_schema": {
            "surface": "Python CLI",
            "data_model": ["Memo(id,text,done)"],
            "modules": ["storage.py", "cli.py"],
            "interfaces": ["memo add/list/done"],
            "technology": ["Python", "JSON file"],
            "test_strategy": ["pytest CLI acceptance tests"],
        },
        "user_scenarios": [
            {
                "id": "scenario-add-list",
                "actor": "사용자",
                "start": "빈 로컬 폴더에서 시작한다.",
                "actions": ["메모를 추가한다.", "목록을 조회한다."],
                "observations": ["추가한 문구와 미완료 상태가 보인다."],
                "error_cases": ["빈 메모는 거부된다."],
            },
            {
                "id": "scenario-complete",
                "actor": "사용자",
                "start": "기존 미완료 메모가 있다.",
                "actions": ["메모 ID로 완료 처리한다."],
                "observations": ["목록에서 완료 상태가 보인다."],
                "error_cases": ["없는 ID는 종료 코드 2로 거부된다."],
            },
        ],
        "implementation_plan": [
            {
                "key": "storage",
                "title": "메모 저장소 구현",
                "depends_on": [],
                "allowed_files": ["src/memo/storage.py", "tests/test_storage.py"],
                "tests": ["tests/test_storage.py"],
                "completion_criteria": ["추가·목록 저장 테스트 통과"],
            },
            {
                "key": "cli",
                "title": "CLI와 완료 흐름 구현",
                "depends_on": ["storage"],
                "allowed_files": ["src/memo/cli.py", "tests/test_cli.py"],
                "tests": ["tests/test_cli.py"],
                "completion_criteria": ["추가·목록·완료 수용시험 통과"],
            },
        ],
    }


@pytest.fixture
def factory(tmp_path: Path):
    company = CompanyOS(tmp_path, db_path=tmp_path / "ledger.sqlite3").initialize()
    idea = company.create_idea("Build a local memo tool.", idempotency_key="idea")
    room = InteractiveCouncil(company, runner=DeterministicExecutives())
    session = room.open(idea.id, idempotency_key="open")
    for number, message in enumerate(("로컬 메모 도구가 필요해.", "완료 표시도 필요해."), 1):
        message_path = tmp_path / f"message-{number}.txt"
        message_path.write_text(message, encoding="utf-8")
        room.turn(
            session["session_id"],
            message_file=message_path,
            idempotency_key=f"turn-{number}",
        )
    room.close(session["session_id"], idempotency_key="close")
    try:
        yield company, ProductFactory(company), session["session_id"]
    finally:
        company.close()


def test_draft_binds_five_artifacts_and_dependency_order(factory, tmp_path):
    _company, product_factory, session_id = factory
    definition = write_json(tmp_path / "definition.json", valid_definition())
    bundle = product_factory.draft(
        session_id, definition_file=definition, idempotency_key="draft-1"
    )

    assert bundle["status"] == "DRAFT"
    assert set(bundle["artifacts"]) == {
        "product_brief",
        "development_schema",
        "user_scenarios",
        "implementation_plan",
        "approval_bundle",
    }
    assert all(len(item["sha256"]) == 64 for item in bundle["artifacts"].values())
    assert [item["key"] for item in bundle["work_orders"]] == ["storage", "cli"]
    assert bundle["target"]["server_deployment"] is False
    with pytest.raises(ValidationError, match="CEO approval"):
        product_factory.execution_plan("synthetic-memo", bundle["bundle_id"])


@pytest.mark.parametrize(
    "mutate,match",
    [
        (
            lambda value: value["implementation_plan"][1].update(
                {"depends_on": ["missing"]}
            ),
            "unresolved dependency",
        ),
        (
            lambda value: value["implementation_plan"][0].update(
                {"depends_on": ["cli"]}
            ),
            "cycle",
        ),
        (
            lambda value: value["implementation_plan"][0].update(
                {"completion_criteria": []}
            ),
            "completion criteria",
        ),
    ],
)
def test_invalid_implementation_plan_is_rejected_before_draft(
    factory, tmp_path, mutate, match
):
    _company, product_factory, session_id = factory
    definition = valid_definition()
    mutate(definition)
    with pytest.raises(ValidationError, match=match):
        product_factory.draft(
            session_id,
            definition_file=write_json(tmp_path / "invalid.json", definition),
            idempotency_key=f"invalid-{match}",
        )


def test_exact_hash_bound_approval_unlocks_only_that_product_version(factory, tmp_path):
    _company, product_factory, session_id = factory
    bundle = product_factory.draft(
        session_id,
        definition_file=write_json(tmp_path / "definition.json", valid_definition()),
        idempotency_key="draft-approval",
    )
    sentence = product_factory.approval_text(bundle["bundle_id"])
    approval_file = tmp_path / "approval.txt"
    approval_file.write_text(sentence, encoding="utf-8")
    approval = product_factory.approve(
        bundle["bundle_id"],
        expected_sha256=bundle["bundle_sha256"],
        approval_file=approval_file,
        idempotency_key="approve-1",
    )

    assert approval["status"] == "APPROVED"
    plan = product_factory.execution_plan("synthetic-memo", bundle["bundle_id"])
    assert [item["key"] for item in plan] == ["storage", "cli"]
    with pytest.raises(ValidationError, match="different product"):
        product_factory.execution_plan("other-product", bundle["bundle_id"])


def test_changed_scenario_after_approval_fails_closed(factory, tmp_path):
    _company, product_factory, session_id = factory
    bundle = product_factory.draft(
        session_id,
        definition_file=write_json(tmp_path / "definition.json", valid_definition()),
        idempotency_key="draft-tamper",
    )
    approval_file = tmp_path / "approval.txt"
    approval_file.write_text(product_factory.approval_text(bundle["bundle_id"]), encoding="utf-8")
    product_factory.approve(
        bundle["bundle_id"],
        expected_sha256=bundle["bundle_sha256"],
        approval_file=approval_file,
        idempotency_key="approve-tamper",
    )
    scenario_path = Path(bundle["artifacts"]["user_scenarios"]["absolute_path"])
    scenario_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValidationError, match="changed after CEO approval"):
        product_factory.execution_plan("synthetic-memo", bundle["bundle_id"])


def test_dashboard_surfaces_council_speeches_and_scenario_approval(factory, tmp_path):
    company, product_factory, session_id = factory
    bundle = product_factory.draft(
        session_id,
        definition_file=write_json(tmp_path / "definition.json", valid_definition()),
        idempotency_key="draft-dashboard",
    )

    snapshot = read_dashboard(company.db_path)

    assert snapshot["council_sessions"][0]["session_id"] == session_id
    assert snapshot["council_sessions"][0]["turn_count"] == 2
    assert set(snapshot["council_sessions"][0]["latest_role_outputs"]) == {
        "cto",
        "cpo",
        "cmo",
    }
    assert snapshot["products"][0]["bundle_id"] == bundle["bundle_id"]
    assert snapshot["products"][0]["attention_kind"] == "SCENARIO_APPROVAL_PENDING"
    page = _render_page(snapshot, "test-token", "http://127.0.0.1:8765/")
    assert "임원 회의" in page
    assert "사용자 시나리오 승인 대기" in page
