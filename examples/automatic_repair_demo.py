"""Synthetic foreground V0.8 demo. Real Claude/Codex only with --execute."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from company_os.application import CompanyOS
from company_os.automation import enqueue, jobs, run_queue
from company_os.fakes import FakeCTO, FakeCPO, FakeCMO
from company_os.utils import atomic_write_text


def prepare(company, *, codex, claude):
    previous = company.store.get_global_state("v08-synthetic-demo")
    if previous:
        return previous
    idea = company.create_idea(
        "Synthetic automatic repair acceptance: writer.render() must return the exact committed "
        "artifact bytes and declared expected content. The deliberately incorrect writer is a "
        "builder fixture. Review correctness of this single function; repair writer.py only. "
        "Do not modify tests or artifact. Not a real customer or OS release review.",
        idempotency_key="v08-demo-idea")
    company.prepare_council(idea.id)
    for role in (FakeCTO(), FakeCPO(), FakeCMO()):
        company.ingest_council_response(idea.id, role=role.role,
                                       response_file=role.write_response(company.root, idea))
    contract = company.compile_council(idea.id, idempotency_key="v08-demo-contract")
    venture = company.record_approval_and_scaffold(contract.contract_id, approval_status="NOT_REQUIRED",
                                                   idempotency_key="v08-demo-venture")
    work = company.first_work_order(venture.id)
    # Declare this synthetic fixture's code requirement in the canonical order
    # before any execution/review. Do not retrofit completed production orders.
    with company.store.transaction() as connection:
        row = connection.execute("SELECT specification_json FROM work_orders WHERE id=?", (work.id,)).fetchone()
        specification = json.loads(row["specification_json"])
        specification["objective"] = "writer.render() must return exactly the declared expected_content; preserve the committed artifact and tests."
        connection.execute("UPDATE work_orders SET specification_json=? WHERE id=?", (json.dumps(specification), work.id))
        company.store.append_event("V08_SYNTHETIC_SCOPE_DECLARED", aggregate_type="WorkOrder", aggregate_id=work.id,
                                   payload={"objective": specification["objective"], "synthetic": True}, connection=connection)
    repository = company.root / "synthetic-product"
    repository.mkdir(exist_ok=True)
    atomic_write_text(repository / work.artifact_relative_path, work.expected_content)
    atomic_write_text(repository / "writer.py", "def render():\n    return 'deliberately incorrect synthetic output'\n")
    atomic_write_text(repository / "test_writer.py",
        "from pathlib import Path\nfrom writer import render\n\n"
        "def test_render_is_text():\n    assert isinstance(render(), str)\n\n"
        "def test_committed_artifact_has_declared_content():\n    assert Path(" + repr(work.artifact_relative_path)
        + ").read_text(encoding='utf-8') == " + repr(work.expected_content) + "\n")
    atomic_write_text(repository / "SCOPE.txt", "Acceptance requirement: writer.render() must equal "
        + repr(work.expected_content) + ". Existing smoke tests are incomplete; inspect the function yourself.\n")
    for args in (("init", "-b", "wo/v08-demo"), ("add", "."),
                 ("-c", "user.name=Company OS Demo", "-c", "user.email=synthetic@example.invalid",
                  "commit", "-m", "deliberately incorrect synthetic writer")):
        subprocess.run(["git", *args], cwd=repository, capture_output=True, check=True, timeout=30)
    atomic_write_text(venture.workspace_path / work.artifact_relative_path, work.expected_content)
    company.verify_existing_artifact(work.id, idempotency_key="v08-demo-artifact")
    plan = {"schema_version": 1, "work_order_id": work.id, "repository": str(repository),
            "allowed_files": ["writer.py"], "new_test_files": ["test_render_exact.py"],
            "test_command": [sys.executable, "-B", "-m", "pytest", "-q"],
            "codex_executable": codex, "claude_executable": claude, "max_repairs": 3}
    job = enqueue(company, plan, idempotency_key="v08-demo-job")
    result = {"work_order_id": work.id, "job_id": job["id"], "repository": str(repository),
              "synthetic": True, "initial_producer": "BUILDER_DETERMINISTIC_NOT_MODEL"}
    company.store.set_global_state("v08-synthetic-demo", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--codex-executable", default="codex")
    parser.add_argument("--claude-executable", default="claude")
    args = parser.parse_args()
    code = Path(__file__).resolve().parents[1]
    root = args.root.resolve()
    if root == code or root.is_relative_to(code):
        parser.error("Runtime root must be outside tracked source")
    with CompanyOS(root) as company:
        result = prepare(company, codex=args.codex_executable, claude=args.claude_executable)
        if args.execute:
            result["steps"] = [{"status": step["status"], "receipt": step.get("last_receipt")}
                               for step in run_queue(company)]
        result["jobs"] = jobs(company)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
