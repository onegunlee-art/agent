from __future__ import annotations

import shutil
import subprocess

import pytest


@pytest.mark.integration
def test_installed_codex_cli_reports_its_version_without_prompting() -> None:
    executable = shutil.which("codex")
    if executable is None:
        pytest.skip("Codex CLI is not installed")
    completed = subprocess.run(
        [executable, "--version"],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=20,
    )
    assert completed.returncode == 0
    assert "codex" in completed.stdout.casefold()
