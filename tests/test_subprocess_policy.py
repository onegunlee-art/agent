from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _subprocess_run_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "run"
    ]


def test_every_test_and_model_executor_subprocess_run_has_a_timeout() -> None:
    paths = sorted((ROOT / "tests").rglob("*.py"))
    paths.extend(sorted((ROOT / "reference" / "v02" / "tests").rglob("*.py")))
    paths.append(ROOT / "src" / "company_os" / "model_executor.py")
    missing: list[str] = []
    for path in paths:
        for call in _subprocess_run_calls(path):
            if not any(keyword.arg == "timeout" for keyword in call.keywords):
                missing.append(f"{path.relative_to(ROOT)}:{call.lineno}")
    assert missing == [], "subprocess.run without timeout: " + ", ".join(missing)


def test_long_lived_popen_uses_bounded_communicate() -> None:
    source = (ROOT / "src" / "company_os" / "model_executor.py").read_text(
        encoding="utf-8"
    )
    assert "process.communicate(timeout=timeout)" in source
