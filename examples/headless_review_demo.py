"""Synthetic V0.7 acceptance demo, never a real customer or AI production claim.

Run with the project's Python. Runtime data must be outside the source repo.
The real Claude call occurs only with --execute; preparing the fixture is free.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from company_os.application import CompanyOS
from company_os.fakes import FakeCTO, FakeCPO, FakeCMO
from company_os.headless_reviewer import run_headless_review
from company_os.utils import atomic_write_text


def prepare(company):
    previous = company.store.get_global_state("v07-synthetic-demo")
    if previous:
        return previous
    idea = company.create_idea(
        "Synthetic V0.7 acceptance fixture: produce one exact-text artifact. "
        "Review the committed artifact and writer.py render() against the declared "
        "expected content. This is a builder-created fixture, not AI-produced work "
        "and not a release review of the Company OS kernel.",
        idempotency_key="v07-demo-idea",
    )
    company.prepare_council(idea.id)
    for role in (FakeCTO(), FakeCPO(), FakeCMO()):
        path = role.write_response(company.root, idea)
        company.ingest_council_response(idea.id, role=role.role, response_file=path)
    contract = company.compile_council(idea.id, idempotency_key="v07-demo-contract")
    venture = company.record_approval_and_scaffold(
        contract.contract_id, approval_status="NOT_REQUIRED", idempotency_key="v07-demo-venture")
    work = company.first_work_order(venture.id)
    repository = company.root / "synthetic-product"
    repository.mkdir(exist_ok=True)
    atomic_write_text(repository / work.artifact_relative_path, work.expected_content)
    atomic_write_text(repository / "writer.py", "def render():\n    return " + repr(work.expected_content) + "\n")
    atomic_write_text(repository / "test_writer.py",
                      "from pathlib import Path\nfrom writer import render\n\n"
                      "def test_render_matches_declared_artifact():\n"
                      "    assert render() == " + repr(work.expected_content) + "\n\n"
                      "def test_committed_artifact_matches_declared_bytes():\n"
                      "    assert Path(" + repr(work.artifact_relative_path) + ").read_text(encoding='utf-8') == render()\n")
    for args in (("init",), ("add", "."), ("-c", "user.name=Company OS Synthetic Demo",
                 "-c", "user.email=synthetic@example.invalid", "commit", "-m", "synthetic V0.7 review target")):
        subprocess.run(["git", *args], cwd=repository, capture_output=True, check=True, timeout=30)
    atomic_write_text(venture.workspace_path / work.artifact_relative_path, work.expected_content)
    company.verify_existing_artifact(work.id, idempotency_key="v07-demo-artifact")
    result = {"work_order_id": work.id, "repository": str(repository),
              "fixture_producer": "BUILDER_DETERMINISTIC_NOT_MODEL", "synthetic": True}
    with company.store.transaction() as conn:
        company.store.set_global_state("v07-synthetic-demo", result, connection=conn)
        company.store.append_event("V07_SYNTHETIC_DEMO_PREPARED", aggregate_type="WorkOrder",
                                   aggregate_id=work.id, payload=result, connection=conn)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--claude-executable", default="claude")
    parser.add_argument("--attempt-key", default="v07-demo-review-1")
    args = parser.parse_args()
    code_root = Path(__file__).resolve().parents[1]
    root = args.root.resolve()
    if root == code_root or root.is_relative_to(code_root):
        parser.error("demo root must be outside the source repository")
    with CompanyOS(root) as company:
        prepared = prepare(company)
        if args.execute:
            prepared["review"] = run_headless_review(
                company, prepared["work_order_id"], repository=Path(prepared["repository"]),
                test_command=[sys.executable, "-B", "-m", "pytest", "-q"],
                executable=args.claude_executable, idempotency_key=args.attempt_key,
            )
        print(json.dumps(prepared, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
