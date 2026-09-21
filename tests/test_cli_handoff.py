from __future__ import annotations

import json
from pathlib import Path

from company_os.application import CompanyOS
from company_os.cli import main
from company_os.fakes import FakeCMO, FakeCPO, FakeCTO
from company_os.utils import atomic_write_text


def invoke(capsys, *arguments: str) -> dict:
    assert main(arguments) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def test_cli_manual_handoff_flow_reaches_waiting_for_opus(
    tmp_path: Path, capsys
) -> None:
    root_args = ("--root", str(tmp_path))
    initialized = invoke(capsys, *root_args, "init")
    assert initialized["status"] == "INITIALIZED"

    idea = invoke(
        capsys,
        *root_args,
        "idea",
        "create",
        "Create one CLI synthetic artifact.",
    )
    idea_id = idea["id"]
    requests = invoke(capsys, *root_args, "council", "prepare", idea_id)
    assert {item["role"] for item in requests} == {"cto", "cpo", "cmo"}

    with CompanyOS(tmp_path) as company:
        idea_model = company.idea(idea_id)
        response_paths = {
            fake.role: fake.write_response(company.root, idea_model)
            for fake in (FakeCTO(), FakeCPO(), FakeCMO())
        }
    for role, response_path in response_paths.items():
        ingested = invoke(
            capsys,
            *root_args,
            "council",
            "ingest",
            idea_id,
            "--role",
            role,
            "--file",
            str(response_path),
        )
        assert ingested["status"] == "INGESTED"

    compiled = invoke(capsys, *root_args, "council", "compile", idea_id)
    assert compiled["gate"]["passed"] is True
    venture = invoke(
        capsys,
        *root_args,
        "venture",
        "scaffold",
        compiled["contract_id"],
    )

    with CompanyOS(tmp_path) as company:
        work_order = company.first_work_order(venture["id"])
        artifact_path = (
            company.venture(venture["id"]).workspace_path
            / work_order.artifact_relative_path
        )
        atomic_write_text(artifact_path, work_order.expected_content)

    run = invoke(
        capsys,
        *root_args,
        "work",
        "verify",
        work_order.id,
    )
    assert run["status"] == "PASS"
    review = invoke(
        capsys,
        *root_args,
        "work",
        "review",
        work_order.id,
    )
    assert review["status"] == "WAITING_FOR_OPUS"
    assert Path(review["json_path"]).is_file()
    assert Path(review["markdown_path"]).is_file()

    status = invoke(capsys, *root_args, "status")
    assert status["waiting_for_opus"][0]["work_order_id"] == work_order.id


def test_cli_exposes_exactly_three_roles(tmp_path: Path, capsys) -> None:
    roles = invoke(capsys, "--root", str(tmp_path), "roles", "list")
    assert roles == {"roles": ["cto", "cpo", "cmo"]}
