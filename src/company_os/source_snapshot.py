"""Deterministic, local source snapshots backed by Git.

Review snapshots are deliberately fail-closed at the repository boundary:
the index and tracked worktree must match ``HEAD``, project-local bytecode,
untracked files, symlinks, and gitlinks are rejected, and ignored paths are
accepted only under explicit local-runtime/cache roots.  The interpreter and
installed dependency environment remain a separate trusted boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from typing import Protocol, runtime_checkable


_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_MANIFEST_HEADER = b"ai-company-os-source-tree-v1\0"
_ALLOWED_IGNORED_ROOTS = {
    b".mypy_cache",
    b".pytest_cache",
    b".ruff_cache",
    b".venv",
    b"artifacts",
    b"evidence",
    b"handoffs",
    b"htmlcov",
    b"logs",
    b"var",
    b"venv",
}
_SCANNED_RUNTIME_ROOTS = (
    "artifacts",
    "evidence",
    "handoffs",
    "logs",
    "var",
)
_ALLOWED_RUNTIME_FILE_SUFFIXES = frozenset(
    {
        ".csv",
        ".db",
        ".db-journal",
        ".db-shm",
        ".db-wal",
        ".gif",
        ".jpeg",
        ".jpg",
        ".json",
        ".jsonl",
        ".log",
        ".md",
        ".pdf",
        ".png",
        ".sqlite",
        ".sqlite-journal",
        ".sqlite-shm",
        ".sqlite-wal",
        ".sqlite3",
        ".sqlite3-journal",
        ".sqlite3-shm",
        ".sqlite3-wal",
        ".tsv",
        ".txt",
        ".webp",
    }
)
_GIT_ROUTING_ENVIRONMENT = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_SUPER_PREFIX",
    "GIT_WORK_TREE",
}


class SourceSnapshotError(RuntimeError):
    """Raised when a trustworthy Git source snapshot cannot be captured."""


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Identity and content digest for a Git-backed source worktree."""

    source_commit: str
    source_tree_oid: str
    source_tree_sha256: str
    dirty: bool


@runtime_checkable
class SourceSnapshotPort(Protocol):
    """Port used by review orchestration to bind requests to source."""

    def capture(self) -> SourceSnapshot:
        """Capture the current source identity and tracked worktree digest."""


@dataclass(frozen=True, slots=True)
class _TrackedEntry:
    path_bytes: bytes
    mode: str
    object_id: str


@dataclass(frozen=True, slots=True)
class _RepositoryMetadata:
    source_commit: str
    source_tree_oid: str
    index: bytes
    status: bytes
    filemode: bool


@dataclass(frozen=True, slots=True)
class _CapturedState:
    metadata: _RepositoryMetadata
    manifest: bytes


class GitSourceSnapshot:
    """Capture source identity from the repository containing ``code_root``.

    ``code_root`` is explicit so callers whose runtime/state root is outside
    the source repository do not accidentally snapshot the current process
    directory.  All Git calls use an argument vector with ``shell=False``.
    """

    def __init__(
        self,
        *,
        code_root: str | Path,
        git_executable: str = "git",
        timeout_seconds: float = 15.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.code_root = Path(code_root).resolve()
        self.git_executable = git_executable
        self.timeout_seconds = timeout_seconds

    def capture(self) -> SourceSnapshot:
        if not self.code_root.is_dir():
            raise SourceSnapshotError(
                f"Source code_root is not a directory: {self.code_root}"
            )

        repository_root = Path(
            self._git_text(self.code_root, "rev-parse", "--show-toplevel")
        ).resolve()
        first = self._capture_once(repository_root)
        second = self._capture_once(repository_root)
        if first != second:
            raise SourceSnapshotError(
                "HEAD, index, or tracked worktree changed while the source "
                "snapshot was captured"
            )
        # Inspect ignored runtime paths before the last detailed Git status.
        # With ``--ignored=traditional`` that status is also a path inventory,
        # so a tracked change or a newly-created ignored file during the scan
        # changes ``final_metadata`` instead of hiding below a collapsed
        # ``!! var/`` entry.
        self._validate_ignored_runtime_files(repository_root)
        final_metadata = self._repository_metadata(repository_root)
        self._validate_clean_status(
            final_metadata.status,
            context="source repository",
        )
        if final_metadata != second.metadata:
            raise SourceSnapshotError(
                "HEAD, index, or tracked worktree changed after the final "
                "source manifest was captured"
            )

        return SourceSnapshot(
            source_commit=first.metadata.source_commit,
            source_tree_oid=first.metadata.source_tree_oid,
            source_tree_sha256=sha256(first.manifest).hexdigest(),
            dirty=False,
        )

    def _capture_once(self, repository_root: Path) -> _CapturedState:
        before = self._repository_metadata(repository_root)
        self._validate_clean_status(before.status, context="source repository")
        self._validate_ignored_runtime_files(repository_root)
        entries = self._tracked_entries(before.index)
        manifest = self._canonical_manifest(
            repository_root,
            entries,
            deleted_paths=set(),
            filemode=before.filemode,
        )
        after = self._repository_metadata(repository_root)
        self._validate_clean_status(after.status, context="source repository")
        self._validate_ignored_runtime_files(repository_root)
        if before != after:
            raise SourceSnapshotError(
                "HEAD, index, or tracked worktree changed while a source "
                "manifest was captured"
            )
        return _CapturedState(metadata=before, manifest=manifest)

    def _repository_metadata(self, repository_root: Path) -> _RepositoryMetadata:
        identities = self._git(
            repository_root,
            "rev-parse",
            "HEAD",
            "HEAD^{tree}",
        ).decode("ascii", errors="strict").splitlines()
        if len(identities) != 2:
            raise SourceSnapshotError("Git returned invalid HEAD identity metadata")
        source_commit = self._object_id(identities[0], label="HEAD commit")
        source_tree_oid = self._object_id(identities[1], label="HEAD tree")
        index = self._git(repository_root, "ls-files", "--stage", "-v", "-z")
        status = self._git(
            repository_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=traditional",
        )
        return _RepositoryMetadata(
            source_commit=source_commit,
            source_tree_oid=source_tree_oid,
            index=index,
            status=status,
            filemode=self._core_filemode(repository_root),
        )

    @classmethod
    def _validate_clean_status(cls, status: bytes, *, context: str) -> None:
        for record in cls._nul_values(status):
            if len(record) < 4 or record[2:3] != b" ":
                raise SourceSnapshotError(
                    f"Git returned malformed status for {context}"
                )
            code = record[:2]
            path_bytes = record[3:]
            if code == b"!!" and cls._allowed_ignored_path(path_bytes):
                continue
            path = path_bytes.decode("utf-8", errors="replace")
            if code == b"!!":
                raise SourceSnapshotError(
                    f"Ignored path outside approved runtime roots in {context}: {path}"
                )
            if code == b"??":
                raise SourceSnapshotError(
                    f"Untracked path prevents a review source snapshot in {context}: {path}"
                )
            raise SourceSnapshotError(
                f"Tracked source must be clean before review in {context}: "
                f"{code.decode('ascii', errors='replace')} {path}"
            )

    @staticmethod
    def _allowed_ignored_path(path_bytes: bytes) -> bool:
        normalized = path_bytes.replace(b"\\", b"/").rstrip(b"/")
        if not normalized:
            return False
        parts = normalized.split(b"/")
        if parts[0] in _ALLOWED_IGNORED_ROOTS:
            return True
        # Project-local bytecode and packaging metadata can affect ordinary
        # imports or plugin discovery.  They must be absent rather than
        # treated as harmless cache files.  Virtual-environment dependencies
        # remain an explicit environment boundary via the root allowlist.
        return False

    @staticmethod
    def _validate_ignored_runtime_files(repository_root: Path) -> None:
        """Allow only inert data formats below ignored local-runtime roots.

        This is intentionally a fail-closed allowlist.  An executable suffix
        denylist can always be bypassed by another interpreter or by an
        extensionless script, so an unknown file type invalidates the source
        snapshot even when Git itself ignores that path.
        """

        for root_name in _SCANNED_RUNTIME_ROOTS:
            runtime_root = repository_root / root_name
            if GitSourceSnapshot._is_link_or_junction(runtime_root):
                raise SourceSnapshotError(
                    "Symlinks and junctions are not permitted for an ignored "
                    f"runtime root: {root_name}"
                )
            if not runtime_root.is_dir():
                continue
            try:
                candidates = runtime_root.rglob("*")
                for candidate in candidates:
                    if GitSourceSnapshot._is_link_or_junction(candidate):
                        relative = candidate.relative_to(repository_root)
                        raise SourceSnapshotError(
                            "Symlinks and junctions are not permitted in an ignored runtime "
                            f"root: {relative.as_posix()}"
                        )
                    if candidate.is_dir():
                        continue
                    if not candidate.is_file():
                        relative = candidate.relative_to(repository_root)
                        raise SourceSnapshotError(
                            "Special filesystem entries are not permitted in an "
                            f"ignored runtime root: {relative.as_posix()}"
                        )
                    suffix = candidate.suffix.casefold()
                    executable_mode = bool(
                        candidate.stat().st_mode
                        & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    )
                    if (
                        suffix not in _ALLOWED_RUNTIME_FILE_SUFFIXES
                        or executable_mode
                    ):
                        relative = candidate.relative_to(repository_root)
                        raise SourceSnapshotError(
                            "Ignored runtime file type is not permitted in a "
                            f"runtime root: {relative.as_posix()}"
                        )
            except SourceSnapshotError:
                raise
            except OSError as exc:
                raise SourceSnapshotError(
                    f"Ignored runtime root could not be inspected: {runtime_root}"
                ) from exc

    @staticmethod
    def _is_link_or_junction(path: Path) -> bool:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction is not None and is_junction())

    def _git(self, cwd: Path, *arguments: str) -> bytes:
        command = [self.git_executable, *arguments]
        environment = self._git_environment()
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                check=False,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise SourceSnapshotError(
                f"Git executable was not found: {self.git_executable}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SourceSnapshotError(
                f"Git command timed out after {self.timeout_seconds:g} seconds"
            ) from exc
        except OSError as exc:
            raise SourceSnapshotError(f"Git command could not start: {exc}") from exc

        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            detail = error[-2000:] if error else "no diagnostic output"
            raise SourceSnapshotError(
                f"Git command failed with exit {completed.returncode}: {detail}"
            )
        return completed.stdout

    @staticmethod
    def _git_environment() -> dict[str, str]:
        environment = os.environ.copy()
        for key in tuple(environment):
            normalized = key.upper()
            if (
                normalized in _GIT_ROUTING_ENVIRONMENT
                or normalized.startswith("GIT_CONFIG")
            ):
                environment.pop(key, None)
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        environment["GIT_TERMINAL_PROMPT"] = "0"
        return environment

    def _git_text(self, cwd: Path, *arguments: str) -> str:
        value = self._git(cwd, *arguments).decode(
            "utf-8", errors="surrogateescape"
        ).strip()
        if not value:
            raise SourceSnapshotError("Git command returned an empty value")
        return value

    @staticmethod
    def _object_id(value: str, *, label: str) -> str:
        if _OBJECT_ID.fullmatch(value) is None:
            raise SourceSnapshotError(f"Git returned an invalid {label} object ID")
        return value.lower()

    def _core_filemode(self, repository_root: Path) -> bool:
        command = [
            self.git_executable,
            "config",
            "--type=bool",
            "--get",
            "core.filemode",
        ]
        environment = self._git_environment()
        try:
            completed = subprocess.run(
                command,
                cwd=repository_root,
                check=False,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SourceSnapshotError("Could not read Git core.filemode") from exc
        if completed.returncode == 1:
            return False
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            raise SourceSnapshotError(
                f"Git command failed with exit {completed.returncode}: {error}"
            )
        return completed.stdout.strip().lower() == b"true"

    def _tracked_entries(self, output: bytes) -> list[_TrackedEntry]:
        entries: list[_TrackedEntry] = []
        for record in self._nul_values(output):
            if len(record) < 3 or record[1:2] != b" ":
                raise SourceSnapshotError("Git returned an invalid index flag")
            flag = record[:1]
            if flag == b"S" or flag.islower():
                raise SourceSnapshotError(
                    "Tracked source uses an unsafe assume-unchanged or "
                    "skip-worktree index flag"
                )
            record = record[2:]
            try:
                metadata, path_bytes = record.split(b"\t", 1)
                mode_bytes, object_bytes, stage = metadata.split(b" ", 2)
                mode = mode_bytes.decode("ascii")
                object_id = object_bytes.decode("ascii").lower()
            except (UnicodeDecodeError, ValueError) as exc:
                raise SourceSnapshotError(
                    "Git returned an invalid index entry"
                ) from exc
            if stage != b"0":
                raise SourceSnapshotError(
                    "Cannot capture a source snapshot with unmerged index entries"
                )
            if not mode.isdigit() or _OBJECT_ID.fullmatch(object_id) is None:
                raise SourceSnapshotError("Git returned an invalid index entry")
            self._validate_git_path(path_bytes)
            entries.append(
                _TrackedEntry(
                    path_bytes=path_bytes,
                    mode=mode,
                    object_id=object_id,
                )
            )
        return sorted(entries, key=lambda item: item.path_bytes)

    def _canonical_manifest(
        self,
        repository_root: Path,
        entries: list[_TrackedEntry],
        *,
        deleted_paths: set[bytes],
        filemode: bool,
    ) -> bytes:
        """Encode sorted path, effective mode, and content SHA-256 entries.

        Every variable-width field is length-prefixed, so all tracked Git
        path bytes—including newlines—have one unambiguous representation.
        """

        manifest = bytearray(_MANIFEST_HEADER)
        effective_entries: list[tuple[bytes, bytes, bytes]] = []
        for entry in entries:
            if entry.path_bytes in deleted_paths:
                continue
            path = self._worktree_path(repository_root, entry.path_bytes)
            mode = self._effective_mode(entry.mode, path, filemode=filemode)
            digest = self._content_sha256(
                repository_root,
                entry,
                path,
            )
            effective_entries.append((entry.path_bytes, mode.encode("ascii"), digest))

        manifest.extend(len(effective_entries).to_bytes(8, "big"))
        for path_bytes, mode_bytes, content_digest in effective_entries:
            manifest.extend(self._frame(path_bytes))
            manifest.extend(self._frame(mode_bytes))
            manifest.extend(content_digest)
        return bytes(manifest)

    def _content_sha256(
        self,
        repository_root: Path,
        entry: _TrackedEntry,
        path: Path,
    ) -> bytes:
        if entry.mode == "160000":
            raise SourceSnapshotError(
                f"Gitlinks are not permitted in a review source snapshot: {path}"
            )
        if entry.mode == "120000" or os.path.islink(path):
            raise SourceSnapshotError(
                f"Tracked symlinks are not permitted in a review source snapshot: {path}"
            )
        if path.is_file():
            return self._sha256_file(path)
        if not path.exists():
            # A clean sparse-checkout path may be absent from the worktree. Its
            # exact staged blob still belongs in the effective tracked tree.
            blob = self._git(repository_root, "cat-file", "blob", entry.object_id)
            return sha256(blob).digest()
        raise SourceSnapshotError(
            f"Tracked source path is not a file, symlink, or gitlink: {path}"
        )

    @staticmethod
    def _effective_mode(index_mode: str, path: Path, *, filemode: bool) -> str:
        if os.path.islink(path):
            return "120000"
        if not path.is_file() or index_mode == "160000":
            return index_mode
        if not filemode:
            return index_mode if index_mode in {"100644", "100755"} else "100644"
        try:
            current_mode = path.stat().st_mode
        except OSError:
            return index_mode
        return "100755" if current_mode & stat.S_IXUSR else "100644"

    @staticmethod
    def _sha256_file(path: Path) -> bytes:
        digest = sha256()
        try:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise SourceSnapshotError(
                f"Tracked source file could not be read: {path}"
            ) from exc
        return digest.digest()

    @staticmethod
    def _frame(value: bytes) -> bytes:
        return len(value).to_bytes(8, "big") + value

    @staticmethod
    def _nul_values(output: bytes) -> list[bytes]:
        if not output:
            return []
        values = output.split(b"\0")
        if values[-1] != b"":
            raise SourceSnapshotError("Git returned malformed NUL-delimited output")
        return values[:-1]

    @staticmethod
    def _validate_git_path(path_bytes: bytes) -> None:
        if not path_bytes:
            raise SourceSnapshotError("Git returned an empty tracked path")
        if path_bytes.startswith((b"/", b"\\")):
            raise SourceSnapshotError("Git returned an absolute tracked path")
        components = path_bytes.replace(b"\\", b"/").split(b"/")
        if any(component in {b"", b".", b".."} for component in components):
            raise SourceSnapshotError("Git returned an unsafe tracked path")

    @classmethod
    def _worktree_path(cls, repository_root: Path, path_bytes: bytes) -> Path:
        cls._validate_git_path(path_bytes)
        path_text = path_bytes.decode("utf-8", errors="surrogateescape")
        parts = PurePosixPath(path_text).parts
        candidate = repository_root.joinpath(*parts)
        try:
            # Resolve only the parent so a symlink leaf can be diagnosed by
            # the explicit fail-closed check instead of being followed here.
            candidate.parent.resolve(strict=False).relative_to(repository_root)
        except (OSError, ValueError) as exc:
            raise SourceSnapshotError(
                "Tracked source path escapes the repository root"
            ) from exc
        return candidate


__all__ = [
    "GitSourceSnapshot",
    "SourceSnapshot",
    "SourceSnapshotError",
    "SourceSnapshotPort",
]
