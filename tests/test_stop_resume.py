from __future__ import annotations

from pathlib import Path

import pytest

from company_os.application import CompanyOS
from company_os.errors import CompanyStoppedError
from company_os.fakes import FakeExecutor

from .helpers import CleanSourceSnapshotter, build_venture


class NeverExecute:
    name = "never-execute"

    def execute(self, work_order, workspace_path):
        raise AssertionError("a completed execution was repeated")


def test_stop_persists_and_resume_continues_after_completed_run(tmp_path: Path) -> None:
    snapshotter = CleanSourceSnapshotter()
    company = CompanyOS(root=tmp_path, source_snapshotter=snapshotter)
    company.initialize()
    _, _, _, work_order = build_venture(company, "synthetic-resume")

    company.stop()
    assert company.is_stopped() is True
    with pytest.raises(CompanyStoppedError):
        company.execute_work_order(
            work_order.id,
            executor=FakeExecutor(),
            idempotency_key="blocked-run",
        )
    company.close()

    restarted = CompanyOS(root=tmp_path, source_snapshotter=snapshotter)
    restarted.initialize()
    assert restarted.is_stopped() is True
    restarted.resume()
    run = restarted.execute_work_order(
        work_order.id,
        executor=FakeExecutor(),
        idempotency_key="completed-before-crash",
    )
    assert run.status == "PASS"
    restarted.close()

    after_crash = CompanyOS(root=tmp_path, source_snapshotter=snapshotter)
    after_crash.initialize()
    review = after_crash.resume_work_order(work_order.id, executor=NeverExecute())

    assert review.status == "WAITING_FOR_OPUS"
    assert len(after_crash.runs_for_work_order(work_order.id)) == 1
