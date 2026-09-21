from __future__ import annotations

import ast
import re
from pathlib import Path

from company_os.roles import ROLE_SPECS


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_and_credentials_are_gitignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for required in (
        "var/",
        "logs/",
        "handoffs/",
        "evidence/",
        "artifacts/",
        ".env",
        "*.db",
        "*.db-wal",
        "*.db-shm",
        "*.sqlite",
        "*.log",
        "*.jsonl",
        "*_request.json",
        "*_response.json",
        "*.pem",
        "*.key",
    ):
        assert required in ignored


def test_source_has_three_roles_and_no_api_or_recursive_codex_integration() -> None:
    assert set(ROLE_SPECS) == {"cto", "cpo", "cmo"}
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src").rglob("*.py")
    ).lower()
    assert "import openai" not in source
    assert "import anthropic" not in source
    assert "subprocess" not in source
    assert "codex cli" not in source


def test_tracked_candidate_files_contain_no_credential_pattern() -> None:
    secret = re.compile(
        r"github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,}|"
        r"sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    )
    ignored_parts = {
        ".git",
        ".venv",
        ".pytest_cache",
        "__pycache__",
        "var",
        "ai_company_os.egg-info",
    }
    text_names = {".gitignore", ".gitattributes"}
    text_suffixes = {".py", ".json", ".md", ".toml", ".txt", ".yaml", ".yml"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or ignored_parts.intersection(path.parts):
            continue
        if path.name not in text_names and path.suffix not in text_suffixes:
            continue
        content = path.read_text(encoding="utf-8")
        assert secret.search(content) is None, path


def test_test_data_is_explicitly_synthetic() -> None:
    fixture_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "tests").rglob("*.py")
    ).lower()
    assert "synthetic" in fixture_source
    forbidden_field = "customer" + "_name"
    assert forbidden_field not in fixture_source

    idea_calls: list[tuple[Path, str]] = []
    for path in (ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            if function_name != "create_idea":
                continue
            expression = node.args[0] if node.args else next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "text"
                ),
                None,
            )
            assert expression is not None, path
            rendered = ast.unparse(expression).lower()
            idea_calls.append((path, rendered))
            assert "synthetic" in rendered, (path, rendered)
    assert idea_calls
