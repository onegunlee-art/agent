"""Canonical local paths and live-ledger safety policy."""

from __future__ import annotations

import os
from pathlib import Path

_SYNC_MARKERS = {"onedrive", "dropbox", "google drive", "googledrive", "icloud drive"}


def is_synchronised_path(path: str | Path) -> bool:
    parts = {part.casefold() for part in Path(path).resolve().parts}
    return any(marker in part for part in parts for marker in _SYNC_MARKERS)


def validate_live_db_path(path: str | Path) -> Path:
    resolved = Path(path).resolve()
    if is_synchronised_path(resolved):
        raise ValueError(
            "live SQLite ledger cannot be stored in a synchronised folder"
        )
    return resolved


def default_ledger_path(root: str | Path) -> Path:
    """Return the production ledger path; non-repository fixtures stay local."""

    configured = os.environ.get("AI_COMPANY_OS_DB")
    if configured:
        return validate_live_db_path(configured)
    root_path = Path(root).resolve()
    if (root_path / ".git").exists():
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local"))
        return validate_live_db_path(local / "ai-company-os" / "ledger.sqlite3")
    return root_path / "var" / "state" / "company.db"
