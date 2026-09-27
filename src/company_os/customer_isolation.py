"""Local customer isolation, backup, and deletion controls for V0.5."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .application import CompanyOS
from .chatbot_line import draft_adversarial_evaluation
from .errors import ConflictError, ValidationError
from .storage import IdempotencyConflict
from .utils import (
    atomic_write_json,
    atomic_write_text,
    canonical_json,
    contained_path,
    payload_hash,
    sha256_file,
    utc_now,
)


_CLIENT_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
_CONFIG_NAME = "client.json"
_DEFAULT_SYNC_FOLDER_MARKERS = ("onedrive", "dropbox", "google drive", "icloud")


@dataclass(frozen=True)
class ClientPolicy:
    client_id: str
    private_root: Path
    client_root: Path
    token_limit: int
    cost_limit_usd: float
    private_markers: tuple[str, ...]
    customer_markers: tuple[str, ...]
    sparse_checkout_patterns: tuple[str, ...]


@dataclass(frozen=True)
class ClientBackupResult:
    path: Path
    sha256: str
    file_count: int
    tree_sha256: str


@dataclass(frozen=True)
class ClientBackupVerification:
    ok: bool
    hash_matches: bool
    client_id: str | None
    file_count: int
    missing: tuple[str, ...]
    mismatched: tuple[str, ...]


def _valid_client_id(client_id: str) -> str:
    value = str(client_id).strip()
    if _CLIENT_ID.fullmatch(value) is None:
        raise ValidationError("client_id must be lowercase kebab-case")
    return value


def _marker_list(values: Sequence[str], *, label: str) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValidationError(f"{label} must be a list of marker strings")
    result = [str(value).strip() for value in values]
    if not result or any(not value for value in result):
        raise ValidationError(f"{label} must contain non-empty marker strings")
    if len(result) != len(set(result)):
        raise ValidationError(f"{label} contains duplicate markers")
    return result


def _validate_private_root(company_root: Path, private_root: Path) -> Path:
    public = company_root.resolve()
    private = private_root.resolve()
    if private == public or public in private.parents or private in public.parents:
        raise ValidationError("private customer repository must be outside public source")
    configured = [
        marker.strip().casefold()
        for marker in re.split(
            r"[;,]", os.environ.get("AI_COMPANY_OS_EXTRA_SYNC_FOLDERS", "")
        )
        if marker.strip()
    ]
    path_text = str(private).casefold()
    if any(
        marker in path_text
        for marker in (*_DEFAULT_SYNC_FOLDER_MARKERS, *configured)
    ):
        raise ValidationError("private customer repository must not be in a sync folder")
    for candidate in (private, *private.parents):
        if (candidate / ".git").exists():
            raise ValidationError(
                "private customer repository registry must not be inside another Git repository"
            )
    return private


def client_sparse_patterns(client_id: str) -> tuple[str, ...]:
    _valid_client_id(client_id)
    return (
        "lines/chatbot",
        "material",
        "src/company_os",
        "tests",
    )


def _client_config(private_root: Path, client_id: str) -> Path:
    client = _valid_client_id(client_id)
    return contained_path(private_root.resolve(), "clients", client, _CONFIG_NAME)


def _git(repository: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValidationError(f"customer repository git command failed: {' '.join(args)}") from exc
    return completed.stdout.strip()


def _copy_public_templates(company_root: Path, client_root: Path) -> None:
    for relative in ("lines/chatbot", "src/company_os", "tests"):
        source = contained_path(company_root, relative)
        if not source.is_dir():
            raise ValidationError(f"public production template is missing: {relative}")
        target = contained_path(client_root, relative)
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".pytest_cache", ".git", "var"
            ),
        )


def _public_template_manifest(company_root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for relative in ("lines/chatbot", "src/company_os", "tests"):
        source_root = contained_path(company_root.resolve(), relative)
        if not source_root.is_dir():
            raise ValidationError(f"public production template is missing: {relative}")
        for source in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
            if source.is_symlink():
                raise ValidationError("public production templates must not contain symlinks")
            if not source.is_file() or source.name.endswith((".pyc", ".pyo")):
                continue
            if any(part in {"__pycache__", ".pytest_cache", ".git", "var"} for part in source.parts):
                continue
            entries.append(
                {
                    "path": source.relative_to(company_root).as_posix(),
                    "sha256": sha256_file(source),
                    "size": source.stat().st_size,
                }
            )
    return entries


def _public_template_sha256(company_root: Path) -> str:
    return _tree_sha256(_public_template_manifest(company_root.resolve()))


def template_sync_approval_text(
    company_root: str | Path,
    private_root: str | Path,
    client_id: str,
) -> str:
    """Return the exact CEO sentence binding a template sync candidate."""

    client = _valid_client_id(client_id)
    policy = load_client_policy(private_root, client)
    before_tree = _git(policy.client_root, "rev-parse", "HEAD^{tree}")
    template_sha = _public_template_sha256(Path(company_root))
    return (
        f"고객 {client}의 템플릿 동기화(현재 tree: {before_tree}, "
        f"공개 템플릿 SHA-256: {template_sha})를 APPROVED로 승인합니다."
    )


def sync_client_templates(
    company: CompanyOS,
    private_root: str | Path,
    client_id: str,
    *,
    approval_file: str | Path,
    idempotency_key: str,
) -> dict[str, Any]:
    """Apply one hash-bound public template snapshot to an isolated client repo."""

    policy = load_client_policy(private_root, client_id, company=company)
    if _git(policy.client_root, "status", "--porcelain"):
        raise ValidationError("client repository must be clean before template sync")
    before_tree = _git(policy.client_root, "rev-parse", "HEAD^{tree}")
    template_sha = _public_template_sha256(company.root)
    expected_approval = template_sync_approval_text(
        company.root, private_root, policy.client_id
    )
    approval = Path(approval_file).resolve()
    if approval.is_symlink() or not approval.is_file():
        raise ValidationError("template sync approval must be a regular file")
    if approval.read_text(encoding="utf-8").strip() != expected_approval:
        raise ValidationError("template sync approval text does not match candidate hashes")
    command_payload = {
        "client_id": policy.client_id,
        "before_tree_oid": before_tree,
        "public_template_sha256": template_sha,
        "approval_sha256": sha256_file(approval),
    }
    try:
        with company.store.transaction() as connection:
            claim = company.store.claim_idempotency(
                idempotency_key,
                "sync_client_templates",
                command_payload,
                connection=connection,
            )
            if not claim.is_new:
                if claim.completed and isinstance(claim.result, dict):
                    return dict(claim.result)
                raise ConflictError("client template sync is already in progress")
            approval_event = company.store.append_event(
                "CLIENT_TEMPLATE_SYNC_APPROVED",
                aggregate_type="Client",
                aggregate_id=policy.client_id,
                payload={**command_payload, "actor": "CEO"},
                connection=connection,
            )
    except IdempotencyConflict as exc:
        raise ConflictError(str(exc)) from exc

    for relative in ("lines/chatbot", "src/company_os", "tests"):
        target = contained_path(policy.client_root, relative)
        if target.exists():
            _remove_repository_tree(target)
        source = contained_path(company.root, relative)
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".pytest_cache", ".git", "var"
            ),
        )
    _git(policy.client_root, "add", "--", "lines/chatbot", "src/company_os", "tests")
    if _git(policy.client_root, "status", "--porcelain"):
        _git(policy.client_root, "commit", "-q", "-m", "Sync reviewed public templates")
    after_tree = _git(policy.client_root, "rev-parse", "HEAD^{tree}")
    with company.store.transaction() as connection:
        event = company.store.append_event(
            "CLIENT_TEMPLATES_SYNCED",
            aggregate_type="Client",
            aggregate_id=policy.client_id,
            payload={
                **command_payload,
                "after_tree_oid": after_tree,
                "approval_event_id": str(approval_event["id"]),
            },
            connection=connection,
        )
        result = {
            "status": "SYNCED",
            "client_id": policy.client_id,
            "before_tree_oid": before_tree,
            "after_tree_oid": after_tree,
            "public_template_sha256": template_sha,
            "approval_event_id": str(approval_event["id"]),
            "event_id": str(event["id"]),
        }
        company.store.complete_idempotency(
            idempotency_key,
            result,
            command="sync_client_templates",
            connection=connection,
        )
    return result


def _initialize_client_repository(
    company_root: Path,
    client_root: Path,
    config: dict[str, Any],
) -> str:
    (client_root / "material").mkdir(parents=True)
    (client_root / "workspace").mkdir()
    (client_root / "material" / ".gitkeep").write_text("", encoding="utf-8")
    (client_root / ".gitignore").write_text(
        "/workspace/\n__pycache__/\n*.py[cod]\n.pytest_cache/\n",
        encoding="utf-8",
        newline="\n",
    )
    _copy_public_templates(company_root, client_root)
    atomic_write_json(client_root / _CONFIG_NAME, config)
    _git(client_root, "init", "-q")
    _git(client_root, "config", "user.name", "AI Company OS")
    _git(client_root, "config", "user.email", "local@ai-company.invalid")
    _git(client_root, "config", "core.autocrlf", "false")
    _git(client_root, "config", "core.filemode", "false")
    _git(client_root, "add", "-A")
    _git(client_root, "commit", "-q", "-m", "Initialize isolated client repository")
    return _git(client_root, "rev-parse", "HEAD^{tree}")


def _remove_repository_tree(path: Path) -> None:
    """Remove a complete client repository, including read-only Git objects."""

    def make_writable_and_retry(function: Any, target: str, _error: Any) -> None:
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        function(target)

    shutil.rmtree(path, onerror=make_writable_and_retry)


def initialize_client(
    company: CompanyOS,
    private_root: str | Path,
    client_id: str,
    *,
    private_markers: Sequence[str],
    customer_markers: Sequence[str],
    token_limit: int,
    cost_limit_usd: float,
    idempotency_key: str,
) -> dict[str, Any]:
    """Create one private client folder while recording only redacted metadata."""

    client = _valid_client_id(client_id)
    private = _validate_private_root(company.root, Path(private_root))
    secrets = _marker_list(private_markers, label="private_markers")
    identities = _marker_list(customer_markers, label="customer_markers")
    if token_limit <= 0:
        raise ValidationError("client token limit must be positive")
    if cost_limit_usd < 0:
        raise ValidationError("client cost limit must be non-negative")
    client_root = contained_path(private, "clients", client)
    patterns = client_sparse_patterns(client)
    marker_digest = payload_hash(
        {"private_markers": secrets, "customer_markers": identities}
    )
    config = {
        "schema_version": 1,
        "client_id": client,
        "token_limit": int(token_limit),
        "cost_limit_usd": float(cost_limit_usd),
        "private_markers": secrets,
        "customer_markers": identities,
        "sparse_checkout_patterns": list(patterns),
        "repository_layout": "ISOLATED_GIT_REPOSITORY",
        "created_at": utc_now(),
    }
    command_payload = {
        "client_id": client,
        "token_limit": int(token_limit),
        "cost_limit_usd": float(cost_limit_usd),
        "marker_set_sha256": marker_digest,
        "sparse_checkout_patterns": list(patterns),
        "repository_layout": "ISOLATED_GIT_REPOSITORY",
    }
    try:
        with company.store.transaction() as connection:
            claim = company.store.claim_idempotency(
                idempotency_key,
                "initialize_client",
                command_payload,
                connection=connection,
            )
            if not claim.is_new:
                if claim.completed and isinstance(claim.result, dict):
                    return dict(claim.result)
                raise ConflictError("client initialization is already in progress")
    except IdempotencyConflict as exc:
        raise ConflictError(str(exc)) from exc

    def release_claim() -> None:
        with company.store.transaction() as connection:
            connection.execute(
                "DELETE FROM idempotency WHERE key = ? AND command = ? "
                "AND status = 'CLAIMED'",
                (idempotency_key, "initialize_client"),
            )

    if client_root.exists():
        release_claim()
        raise ValidationError(f"client already exists: {client}")
    try:
        repository_tree_oid = _initialize_client_repository(
            company.root, client_root, config
        )
        with company.store.transaction() as connection:
            event = company.store.append_event(
                "CUSTOMER_ISOLATION_CREATED",
                aggregate_type="Client",
                aggregate_id=client,
                payload={
                    **command_payload,
                    "client_path": f"clients/{client}",
                    "repository_tree_oid": repository_tree_oid,
                    "raw_markers_recorded": False,
                },
                connection=connection,
            )
            result = {
                "status": "CREATED",
                "client_id": client,
                "client_path": f"clients/{client}",
                "event_id": str(event["id"]),
                "marker_set_sha256": marker_digest,
                "repository_tree_oid": repository_tree_oid,
            }
            company.store.complete_idempotency(
                idempotency_key,
                result,
                command="initialize_client",
                connection=connection,
            )
        return result
    except BaseException:
        if client_root.exists():
            _remove_repository_tree(client_root)
        release_claim()
        raise


def load_client_policy(
    private_root: str | Path,
    client_id: str,
    *,
    company: CompanyOS | None = None,
) -> ClientPolicy:
    private = Path(private_root).resolve()
    client = _valid_client_id(client_id)
    source = _client_config(private, client)
    if source.is_symlink() or not source.is_file():
        raise ValidationError(f"client policy not found: {client}")
    try:
        config = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("client policy must be valid UTF-8 JSON") from exc
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValidationError("unsupported client policy schema")
    if config.get("client_id") != client:
        raise ValidationError("client policy identity mismatch")
    if config.get("repository_layout") != "ISOLATED_GIT_REPOSITORY":
        raise ValidationError("client policy does not use an isolated Git repository")
    if not (source.parent / ".git").is_dir():
        raise ValidationError("client Git repository is missing")
    if Path(_git(source.parent, "rev-parse", "--show-toplevel")).resolve() != source.parent:
        raise ValidationError("client policy is not the root of its Git repository")
    expected = client_sparse_patterns(client)
    actual = tuple(str(value) for value in config.get("sparse_checkout_patterns", []))
    if actual != expected:
        raise ValidationError("client policy sparse patterns were modified")
    try:
        token_limit = int(config["token_limit"])
        cost_limit = float(config["cost_limit_usd"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("client policy limits are invalid") from exc
    if token_limit <= 0 or cost_limit < 0:
        raise ValidationError("client policy limits are invalid")
    policy = ClientPolicy(
        client,
        private,
        source.parent,
        token_limit,
        cost_limit,
        tuple(_marker_list(config.get("private_markers", []), label="private_markers")),
        tuple(_marker_list(config.get("customer_markers", []), label="customer_markers")),
        expected,
    )
    if company is not None:
        events = [
            event
            for event in company.events()
            if event.get("aggregate_type") == "Client"
            and event.get("aggregate_id") == client
            and event.get("event_type")
            in {"CUSTOMER_ISOLATION_CREATED", "CUSTOMER_DATA_DELETED"}
        ]
        if not events or events[-1]["event_type"] != "CUSTOMER_ISOLATION_CREATED":
            raise ValidationError("active client policy was not found in canonical ledger")
        recorded = events[-1]["payload"]
        marker_digest = payload_hash(
            {
                "private_markers": list(policy.private_markers),
                "customer_markers": list(policy.customer_markers),
            }
        )
        if (
            recorded.get("marker_set_sha256") != marker_digest
            or int(recorded.get("token_limit", -1)) != policy.token_limit
            or float(recorded.get("cost_limit_usd", -1)) != policy.cost_limit_usd
            or tuple(recorded.get("sparse_checkout_patterns", []))
            != policy.sparse_checkout_patterns
            or recorded.get("repository_layout") != "ISOLATED_GIT_REPOSITORY"
        ):
            raise ValidationError("private client policy differs from canonical ledger")
    return policy


def validate_client_execution(
    policy: ClientPolicy,
    *,
    repository: str | Path,
    sparse_checkout_patterns: Sequence[str],
    token_limit: int,
    cost_limit_usd: float,
) -> tuple[str, ...]:
    """Fail closed before a model process can see another client or overspend."""

    if Path(repository).resolve() != policy.client_root:
        raise ValidationError("customer execution repository is not its isolated client root")
    actual = tuple(str(item).replace("\\", "/").rstrip("/") for item in sparse_checkout_patterns)
    if actual != policy.sparse_checkout_patterns:
        raise ValidationError("customer sparse checkout does not exactly match its policy")
    if token_limit > policy.token_limit:
        raise ValidationError("WorkOrder token limit exceeds client token limit")
    if cost_limit_usd > policy.cost_limit_usd:
        raise ValidationError("WorkOrder cost limit exceeds client cost limit")
    return actual


def configure_customer_sparse_checkout(
    workspace: str | Path,
    patterns: Sequence[str],
) -> None:
    root = Path(workspace).resolve()
    subprocess.run(
        ["git", "sparse-checkout", "init", "--cone"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    subprocess.run(
        ["git", "sparse-checkout", "set", "--", *patterns],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def isolated_evaluation_draft(
    private_root: str | Path,
    client_id: str,
    intake: dict[str, Any],
    *,
    company: CompanyOS | None = None,
) -> dict[str, Any]:
    """Build a DRAFT with this client's secrets and every other client marker."""

    private = Path(private_root).resolve()
    policy = load_client_policy(private, client_id, company=company)
    if intake.get("customer_id") != policy.client_id:
        raise ValidationError("evaluation intake client_id does not match policy")
    other_markers: list[str] = []
    clients_root = contained_path(private, "clients")
    for entry in sorted(clients_root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir() or entry.name == policy.client_id:
            continue
        other = load_client_policy(private, entry.name, company=company)
        other_markers.extend(other.private_markers)
        other_markers.extend(other.customer_markers)
    isolated = deepcopy(intake)
    isolated["private_markers"] = list(policy.private_markers)
    isolated["other_customer_markers"] = sorted(set(other_markers))
    return draft_adversarial_evaluation(isolated)


def save_isolated_evaluation_draft(
    private_root: str | Path,
    client_id: str,
    intake_path: str | Path,
    output_path: str | Path,
    *,
    company: CompanyOS | None = None,
) -> dict[str, Any]:
    """Write an isolated DRAFT only inside the selected client's folder."""

    policy = load_client_policy(private_root, client_id, company=company)
    source = Path(intake_path).resolve()
    destination = Path(output_path).resolve()
    if policy.client_root not in source.parents:
        raise ValidationError("evaluation intake must stay inside the client folder")
    if policy.client_root not in destination.parents:
        raise ValidationError("evaluation output must stay inside the client folder")
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 1024 * 1024:
        raise ValidationError("evaluation intake must be a regular file under 1 MiB")
    if destination.is_symlink():
        raise ValidationError("evaluation output must not be a symlink")
    try:
        intake = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("evaluation intake must be valid UTF-8 JSON") from exc
    if not isinstance(intake, dict):
        raise ValidationError("evaluation intake must contain one JSON object")
    draft = isolated_evaluation_draft(
        private_root, client_id, intake, company=company
    )
    atomic_write_json(destination, draft)
    return {
        "status": "DRAFT",
        "client_id": policy.client_id,
        "evaluation_path": destination.relative_to(policy.private_root).as_posix(),
        "evaluation_sha256": sha256_file(destination),
        "case_count": len(draft["cases"]),
        "critical_forbidden_count": len(draft["critical_forbidden"]),
        "raw_markers_returned": False,
    }


def _file_manifest(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for source in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if source.is_symlink():
            raise ValidationError("customer data must not contain symlinks")
        if source.is_file():
            entries.append(
                {
                    "path": source.relative_to(root).as_posix(),
                    "sha256": sha256_file(source),
                    "size": source.stat().st_size,
                }
            )
    return entries


def _tree_sha256(entries: Sequence[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_json(list(entries)).encode("utf-8")).hexdigest()


def backup_client_data(
    private_root: str | Path,
    client_id: str,
    backup_dir: str | Path,
    *,
    timestamp: str | None = None,
    company: CompanyOS | None = None,
) -> ClientBackupResult:
    private = Path(private_root).resolve()
    policy = load_client_policy(private, client_id, company=company)
    entries = _file_manifest(policy.client_root)
    destination = contained_path(Path(backup_dir).resolve(), policy.client_id)
    destination.mkdir(parents=True, exist_ok=True)
    stamp = timestamp or time.strftime("%Y%m%d-%H%M%S")
    target = destination / f"client-data-{policy.client_id}-{stamp}.zip"
    if target.exists():
        raise FileExistsError(target)
    manifest = {
        "schema_version": 1,
        "client_id": policy.client_id,
        "tree_sha256": _tree_sha256(entries),
        "files": entries,
    }
    with zipfile.ZipFile(
        target,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode(
                "utf-8"
            ),
        )
        for item in entries:
            archive.write(
                policy.client_root / str(item["path"]),
                f"files/{item['path']}",
            )
    digest = sha256_file(target)
    atomic_write_text(target.with_suffix(".sha256"), f"{digest}  {target.name}\n")
    return ClientBackupResult(target, digest, len(entries), str(manifest["tree_sha256"]))


def _safe_archive_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = Path(normalized.replace("/", os.sep))
    if path.is_absolute() or ".." in path.parts:
        raise ValidationError(f"unsafe customer backup entry: {name}")
    return normalized


def verify_client_backup(bundle_path: str | Path) -> ClientBackupVerification:
    source = Path(bundle_path).resolve()
    sidecar = source.with_suffix(".sha256")
    if not source.is_file() or not sidecar.is_file():
        return ClientBackupVerification(False, False, None, 0, (), ("bundle",))
    recorded = sidecar.read_text(encoding="utf-8").split()
    hash_matches = bool(recorded) and recorded[0] == sha256_file(source)
    missing: list[str] = []
    mismatched: list[str] = []
    client_id: str | None = None
    file_count = 0
    try:
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValidationError("customer backup contains duplicate entries")
            if "manifest.json" not in names:
                raise ValidationError("customer backup manifest is missing")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("schema_version") != 1:
                raise ValidationError("unsupported customer backup schema")
            client_id = _valid_client_id(str(manifest["client_id"]))
            entries = manifest.get("files")
            if not isinstance(entries, list):
                raise ValidationError("customer backup files manifest is invalid")
            file_count = len(entries)
            expected = {"manifest.json"}
            for item in entries:
                archive_name = _safe_archive_name(f"files/{item['path']}")
                expected.add(archive_name)
                if archive_name not in names:
                    missing.append(archive_name)
                    continue
                content = archive.read(archive_name)
                if (
                    hashlib.sha256(content).hexdigest() != item["sha256"]
                    or len(content) != int(item["size"])
                ):
                    mismatched.append(archive_name)
            for unexpected in sorted(set(names) - expected):
                mismatched.append(f"unexpected:{unexpected}")
            if _tree_sha256(entries) != manifest.get("tree_sha256"):
                mismatched.append("tree_sha256")
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ):
        mismatched.append("bundle")
    return ClientBackupVerification(
        hash_matches and not missing and not mismatched,
        hash_matches,
        client_id,
        file_count,
        tuple(missing),
        tuple(mismatched),
    )


def restore_client_backup(
    bundle_path: str | Path,
    new_private_root: str | Path,
) -> Path:
    source = Path(bundle_path).resolve()
    checked = verify_client_backup(source)
    if not checked.ok or checked.client_id is None:
        raise ValidationError("customer backup verification failed")
    destination = contained_path(
        Path(new_private_root).resolve(), "clients", checked.client_id
    )
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="client-restore-") as temp_name:
        staging = Path(temp_name) / checked.client_id
        staging.mkdir()
        with zipfile.ZipFile(source) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            for item in manifest["files"]:
                relative = Path(_safe_archive_name(str(item["path"])).replace("/", os.sep))
                target = contained_path(staging, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(f"files/{item['path']}"))
        (staging / "material").mkdir(exist_ok=True)
        (staging / "workspace").mkdir(exist_ok=True)
        shutil.move(str(staging), str(destination))
    load_client_policy(Path(new_private_root), checked.client_id)
    return destination


def delete_client_data(
    company: CompanyOS,
    private_root: str | Path,
    client_id: str,
    *,
    confirmation: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Delete one client's active data and backups, then retain a hash-only proof."""

    private = Path(private_root).resolve()
    client = _valid_client_id(client_id)
    if confirmation != client:
        raise ValidationError("client deletion confirmation does not match client_id")
    with company.store.transaction(immediate=False) as connection:
        prior = connection.execute(
            "SELECT command, status, result_json FROM idempotency WHERE key = ?",
            (idempotency_key,),
        ).fetchone()
    if prior is not None:
        if prior["command"] != "delete_client_data":
            raise ConflictError("deletion idempotency key belongs to another command")
        if prior["status"] == "COMPLETED" and prior["result_json"]:
            result = json.loads(prior["result_json"])
            if result.get("client_id") != client:
                raise ConflictError("deletion idempotency key belongs to another client")
            return dict(result)
        raise ConflictError("client deletion is already in progress or needs recovery")

    policy = load_client_policy(private, client, company=company)
    repository_entries = _file_manifest(policy.client_root)
    git_history_entries = [
        entry
        for entry in repository_entries
        if entry["path"] == ".git" or str(entry["path"]).startswith(".git/")
    ]
    active_entries = [
        entry for entry in repository_entries if entry not in git_history_entries
    ]
    backup_root = contained_path(private, "backups", policy.client_id)
    backup_entries = _file_manifest(backup_root) if backup_root.is_dir() else []
    combined = (
        [{**entry, "scope": "active"} for entry in active_entries]
        + [{**entry, "scope": "backup"} for entry in backup_entries]
        + [{**entry, "scope": "git-history"} for entry in git_history_entries]
    )
    tree_digest = _tree_sha256(combined)
    deleted_bytes = sum(int(item["size"]) for item in combined)
    certificate_name = f"{policy.client_id}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    certificate_path = contained_path(private, "deletion-evidence", certificate_name)
    command_payload = {
        "client_id": policy.client_id,
        "pre_delete_tree_sha256": tree_digest,
        "deleted_file_count": len(combined),
        "deleted_bytes": deleted_bytes,
        "deletion_scope_counts": {
            "active": len(active_entries),
            "backup": len(backup_entries),
            "git_history": len(git_history_entries),
        },
    }

    try:
        with company.store.transaction() as connection:
            claim = company.store.claim_idempotency(
                idempotency_key,
                "delete_client_data",
                command_payload,
                connection=connection,
            )
            if not claim.is_new:
                raise ConflictError("client deletion is already claimed")
    except IdempotencyConflict as exc:
        raise ConflictError(str(exc)) from exc

    try:
        _remove_repository_tree(policy.client_root)
        if backup_root.exists():
            _remove_repository_tree(backup_root)
        deleted_scopes = {
            "active": {
                "file_count": len(active_entries),
                "bytes": sum(int(item["size"]) for item in active_entries),
                "removed": not policy.client_root.exists(),
            },
            "backup": {
                "file_count": len(backup_entries),
                "bytes": sum(int(item["size"]) for item in backup_entries),
                "removed": not backup_root.exists(),
            },
            "git_history": {
                "file_count": len(git_history_entries),
                "bytes": sum(int(item["size"]) for item in git_history_entries),
                "removed": not policy.client_root.exists(),
                "repository_directory_removed": not policy.client_root.exists(),
            },
        }
        deletion_complete = all(
            bool(scope["removed"]) for scope in deleted_scopes.values()
        )
        if not deletion_complete:
            raise RuntimeError("customer deletion did not remove every managed scope")
        certificate = {
            "schema_version": 1,
            "status": "DELETED",
            **command_payload,
            "deleted_scopes": deleted_scopes,
            "deleted_at": utc_now(),
            "raw_customer_data_retained": not deletion_complete,
        }
        atomic_write_json(certificate_path, certificate)
        certificate_sha = sha256_file(certificate_path)
        with company.store.transaction() as connection:
            event = company.store.append_event(
                "CUSTOMER_DATA_DELETED",
                aggregate_type="Client",
                aggregate_id=policy.client_id,
                payload={
                    **command_payload,
                    "deleted_scopes": deleted_scopes,
                    "certificate_path": f"deletion-evidence/{certificate_name}",
                    "certificate_sha256": certificate_sha,
                    "actor": "CEO",
                },
                connection=connection,
            )
            result = {
                "status": "DELETED",
                "client_id": policy.client_id,
                "event_id": str(event["id"]),
                "certificate_path": str(certificate_path),
                "certificate_sha256": certificate_sha,
                "deleted_file_count": len(combined),
            }
            company.store.complete_idempotency(
                idempotency_key,
                result,
                command="delete_client_data",
                connection=connection,
            )
        return result
    except BaseException:
        # A still-present repository means no truthful completion was recorded and
        # the exact command can safely be retried.  If deletion already removed the
        # repository, retain CLAIMED so recovery cannot silently issue a false retry.
        if policy.client_root.exists():
            with company.store.transaction() as connection:
                connection.execute(
                    "DELETE FROM idempotency WHERE key = ? AND command = ? "
                    "AND status = 'CLAIMED'",
                    (idempotency_key, "delete_client_data"),
                )
        raise


def recover_client_deletion(
    company: CompanyOS,
    private_root: str | Path,
    client_id: str,
    *,
    deletion_idempotency_key: str,
) -> dict[str, Any]:
    """Recover a deletion interrupted between filesystem removal and Event commit."""

    private = Path(private_root).resolve()
    client = _valid_client_id(client_id)
    with company.store.transaction(immediate=False) as connection:
        row = connection.execute(
            "SELECT * FROM idempotency WHERE key = ?",
            (deletion_idempotency_key,),
        ).fetchone()
    if row is None or row["command"] != "delete_client_data":
        raise ValidationError("claimed client deletion was not found")
    if row["status"] == "COMPLETED" and row["result_json"]:
        result = json.loads(row["result_json"])
        if result.get("client_id") != client:
            raise ConflictError("deletion idempotency key belongs to another client")
        return dict(result)
    request = json.loads(row["request_json"] or "{}")
    if request.get("client_id") != client:
        raise ConflictError("deletion idempotency key belongs to another client")

    client_root = contained_path(private, "clients", client)
    backup_root = contained_path(private, "backups", client)
    if client_root.exists() or backup_root.exists():
        with company.store.transaction() as connection:
            company.store.append_event(
                "CUSTOMER_DELETION_RECOVERY_RESET",
                aggregate_type="Client",
                aggregate_id=client,
                payload={
                    "deletion_idempotency_key": deletion_idempotency_key,
                    "active_data_remains": client_root.exists(),
                    "backup_data_remains": backup_root.exists(),
                    "status": "RETRY_ALLOWED",
                },
                connection=connection,
            )
            connection.execute(
                "DELETE FROM idempotency WHERE key = ? AND command = ? "
                "AND status = 'CLAIMED'",
                (deletion_idempotency_key, "delete_client_data"),
            )
        return {"status": "RETRY_ALLOWED", "client_id": client}

    counts = request.get("deletion_scope_counts", {})
    deleted_scopes = {
        "active": {
            "file_count": int(counts.get("active", 0)),
            "removed": True,
        },
        "backup": {
            "file_count": int(counts.get("backup", 0)),
            "removed": True,
        },
        "git_history": {
            "file_count": int(counts.get("git_history", 0)),
            "removed": True,
            "repository_directory_removed": True,
        },
    }
    certificate_name = (
        f"{client}-recovered-{str(row['payload_hash'])[:12]}.json"
    )
    certificate_path = contained_path(private, "deletion-evidence", certificate_name)
    certificate = {
        "schema_version": 1,
        "status": "DELETED",
        **request,
        "deleted_scopes": deleted_scopes,
        "deleted_at": utc_now(),
        "raw_customer_data_retained": False,
        "recovered_from_interruption": True,
        "deletion_idempotency_key": deletion_idempotency_key,
    }
    if not certificate_path.exists():
        atomic_write_json(certificate_path, certificate)
    certificate_sha = sha256_file(certificate_path)
    with company.store.transaction() as connection:
        event = company.store.append_event(
            "CUSTOMER_DATA_DELETED",
            aggregate_type="Client",
            aggregate_id=client,
            payload={
                **request,
                "deleted_scopes": deleted_scopes,
                "certificate_path": f"deletion-evidence/{certificate_name}",
                "certificate_sha256": certificate_sha,
                "actor": "CEO",
                "recovered_from_interruption": True,
            },
            connection=connection,
        )
        result = {
            "status": "DELETED",
            "client_id": client,
            "event_id": str(event["id"]),
            "certificate_path": str(certificate_path),
            "certificate_sha256": certificate_sha,
            "deleted_file_count": int(request.get("deleted_file_count", 0)),
            "recovered": True,
        }
        company.store.complete_idempotency(
            deletion_idempotency_key,
            result,
            command="delete_client_data",
            connection=connection,
        )
    return result
