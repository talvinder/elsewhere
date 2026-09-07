"""Provider-neutral source packaging and artifact lifecycle operations."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_capacity.journey import source_fingerprint
from agent_capacity.s3_artifacts import (
    cleanup as cleanup_s3_artifact,
    download as download_s3_artifact,
    prepare_result as prepare_s3_result,
    prepare_source as prepare_s3_source,
)

SOURCE_EXCLUDES = {
    ".git", ".next", ".venv", "venv", "node_modules", "dist", "build",
    "__pycache__", ".ds_store", ".env", ".env.local", ".env.production",
    ".aws", ".azure", ".gnupg", ".ssh", ".terraform", ".pulumi",
}
SOURCE_EXCLUDE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
SOURCE_EXCLUDE_NAMES = {
    ".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json",
    "id_rsa", "id_ed25519", "service-account.json", "secrets.json",
    ".agent-capacity-manifest.json",
}
MAX_SOURCE_FILE_BYTES = 100 * 1024 * 1024


def source_file_allowed(root: Path, path: Path) -> tuple[bool, str | None]:
    relative = path.relative_to(root)
    lower_name = path.name.lower()
    if (
        lower_name.startswith(".env")
        or lower_name in SOURCE_EXCLUDE_NAMES
        or lower_name.startswith("service-account-")
        or path.suffix.lower() in SOURCE_EXCLUDE_SUFFIXES
    ):
        return False, "sensitive file pattern"
    if any(part.lower() in SOURCE_EXCLUDES for part in relative.parts):
        return False, "excluded path"
    if path.is_symlink():
        return False, "symlink"
    if not path.is_file():
        return False, "not a regular file"
    if path.stat().st_size > MAX_SOURCE_FILE_BYTES:
        return False, "file exceeds 100 MB"
    return True, None


def _read_stable_source_file(path: Path) -> tuple[bytes | None, os.stat_result | None, str | None]:
    """Read one regular file without following a last-moment symlink replacement."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None, None, "file changed during packaging"
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return None, None, "not a regular file"
        if before.st_size > MAX_SOURCE_FILE_BYTES:
            return None, None, "file exceeds 100 MB"
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(MAX_SOURCE_FILE_BYTES + 1)
        after = os.fstat(descriptor)
    except OSError:
        return None, None, "file changed during packaging"
    finally:
        os.close(descriptor)
    if len(content) > MAX_SOURCE_FILE_BYTES or after.st_size > MAX_SOURCE_FILE_BYTES:
        return None, None, "file exceeds 100 MB"
    identity = lambda value: (
        value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns
    )
    if identity(before) != identity(after) or len(content) != after.st_size:
        return None, None, "file changed during packaging"
    return content, after, None


def package_source(source_path: str, job_id: str) -> tuple[Path, dict[str, Any]]:
    root = Path(source_path).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"source path is not a directory: {root}")
    output_file = tempfile.NamedTemporaryFile(
        prefix=f"elsewhere-source-{job_id}-",
        suffix=".tar.gz",
        dir=tempfile.gettempdir(),
        delete=False,
    )
    output = Path(output_file.name)
    files = []
    skipped = []
    total_bytes = 0
    try:
        with output_file, tarfile.open(fileobj=output_file, mode="w:gz") as archive:
            for path in sorted(root.rglob("*")):
                allowed, reason = source_file_allowed(root, path)
                relative = path.relative_to(root).as_posix()
                if not allowed:
                    if path.is_file() and reason != "excluded path":
                        skipped.append({"path": relative, "reason": reason})
                    continue
                content, snapshot, read_error = _read_stable_source_file(path)
                if read_error or content is None or snapshot is None:
                    skipped.append({
                        "path": relative,
                        "reason": read_error or "file changed during packaging",
                    })
                    continue
                digest = hashlib.sha256(content).hexdigest()
                size = len(content)
                files.append({"path": relative, "sha256": digest, "size": size})
                total_bytes += size
                info = tarfile.TarInfo(relative)
                info.size = size
                info.mtime = int(snapshot.st_mtime)
                info.mode = snapshot.st_mode & 0o777
                archive.addfile(info, io.BytesIO(content))
            manifest = {
                "version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "source_name": root.name,
                "files": files,
                "content_sha256": source_fingerprint(files),
                "skipped": skipped,
                "total_bytes": total_bytes,
            }
            payload = json.dumps(manifest, indent=2, sort_keys=True).encode()
            info = tarfile.TarInfo(".agent-capacity-manifest.json")
            info.size = len(payload)
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(payload))
    except BaseException:
        output_file.close()
        output.unlink(missing_ok=True)
        raise
    return output, manifest


def azure_blob_config(config: dict[str, Any]) -> dict[str, Any]:
    values = config.get("artifact_store", {})
    if values.get("provider") != "azure-blob":
        raise SystemExit("the configured artifact store is not supported")
    if not values.get("account"):
        raise SystemExit("artifact_store.account is required for local source transport")
    if shutil.which("az") is None:
        raise SystemExit("Azure CLI is required for the azure-blob artifact store")
    return values


def az_with_subscription(command: list[str], subscription: str | None) -> list[str]:
    return command + (["--subscription", subscription] if subscription else [])


def prepare_source_artifact(job: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    bundle, manifest = package_source(job["source_path"], job["id"])
    if config.get("artifact_store", {}).get("provider") == "tigris":
        try:
            return prepare_s3_source(bundle, manifest, job, config["artifact_store"])
        finally:
            bundle.unlink(missing_ok=True)
    values = azure_blob_config(config)
    account = values["account"]
    container = values.get("container", "elsewhere-artifacts")
    subscription = values.get("subscription")
    blob = f"sources/{job['id']}.tar.gz"
    try:
        create = az_with_subscription([
            "az", "storage", "container", "create", "--account-name", account,
            "--name", container, "--auth-mode", "login", "--only-show-errors", "-o", "none",
        ], subscription)
        subprocess.run(create, check=True)
        upload = az_with_subscription([
            "az", "storage", "blob", "upload", "--account-name", account,
            "--container-name", container, "--name", blob, "--file", str(bundle),
            "--auth-mode", "login", "--overwrite", "--only-show-errors", "-o", "none",
        ], subscription)
        subprocess.run(upload, check=True)
        expiry = datetime.now(UTC) + timedelta(minutes=int(values.get("sas_ttl_minutes", 60)))
        sas_command = az_with_subscription([
            "az", "storage", "blob", "generate-sas", "--account-name", account,
            "--container-name", container, "--name", blob, "--permissions", "r",
            "--expiry", expiry.strftime("%Y-%m-%dT%H:%MZ"), "--auth-mode", "login",
            "--as-user", "--https-only", "-o", "tsv",
        ], subscription)
        sas = subprocess.check_output(sas_command, text=True).strip()
    except Exception as upload_error:
        rollback = cleanup_artifact({
            "provider": "azure-blob", "account": account, "container": container, "blob": blob,
        }, config)
        if not rollback.get("verified_absent"):
            raise RuntimeError(
                f"source preparation failed and cleanup could not be verified for {blob}"
            ) from upload_error
        raise
    finally:
        bundle.unlink(missing_ok=True)
    return {
        "provider": "azure-blob",
        "account": account,
        "container": container,
        "blob": blob,
        "url": f"https://{account}.blob.core.windows.net/{container}/{blob}?{sas}",
        "expires_at": expiry.isoformat(),
        "manifest": {
            "file_count": len(manifest["files"]),
            "content_sha256": manifest["content_sha256"],
            "skipped_count": len(manifest["skipped"]),
            "total_bytes": manifest["total_bytes"],
            "skipped": manifest["skipped"],
        },
    }


def prepare_result_artifact(
    job: dict[str, Any], config: dict[str, Any], result_paths: list[str]
) -> dict[str, Any]:
    if config.get("artifact_store", {}).get("provider") == "tigris":
        return prepare_s3_result(job, config["artifact_store"], result_paths)
    values = azure_blob_config(config)
    account = values["account"]
    container = values.get("container", "elsewhere-artifacts")
    subscription = values.get("subscription")
    blob = f"results/{job['id']}.tar.gz"
    subprocess.run(az_with_subscription([
        "az", "storage", "container", "create", "--account-name", account,
        "--name", container, "--auth-mode", "login", "--only-show-errors", "-o", "none",
    ], subscription), check=True)
    ttl_minutes = max(
        int(values.get("sas_ttl_minutes", 60)),
        math.ceil(int(job["max_runtime_seconds"]) / 60) + 30,
    )
    expiry = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
    sas = subprocess.check_output(az_with_subscription([
        "az", "storage", "blob", "generate-sas", "--account-name", account,
        "--container-name", container, "--name", blob, "--permissions", "cw",
        "--expiry", expiry.strftime("%Y-%m-%dT%H:%MZ"), "--auth-mode", "login",
        "--as-user", "--https-only", "-o", "tsv",
    ], subscription), text=True).strip()
    return {
        "provider": "azure-blob", "account": account, "container": container, "blob": blob,
        "url": f"https://{account}.blob.core.windows.net/{container}/{blob}?{sas}",
        "expires_at": expiry.isoformat(), "requested_paths": result_paths, "state": "awaiting_upload",
    }


def _artifact_values(artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    return {**config.get("artifact_store", {}), **{
        key: artifact[key] for key in ("bucket", "endpoint", "region") if key in artifact
    }}


def cleanup_artifact(artifact: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if artifact.get("provider") == "tigris":
        return cleanup_s3_artifact(artifact, _artifact_values(artifact, config))
    if artifact.get("provider") != "azure-blob":
        return {"deleted": False, "reason": "unsupported artifact provider"}
    values = config.get("artifact_store", {})
    result = subprocess.run(az_with_subscription([
        "az", "storage", "blob", "delete", "--account-name", artifact["account"],
        "--container-name", artifact["container"], "--name", artifact["blob"],
        "--auth-mode", "login", "--only-show-errors", "-o", "none",
    ], values.get("subscription")), text=True, capture_output=True)
    already_absent = result.returncode != 0 and (
        "BlobNotFound" in result.stderr or "specified blob does not exist" in result.stderr
    )
    if result.returncode and not already_absent:
        return {
            "deleted": False, "verified_absent": False, "returncode": result.returncode,
            "error": "artifact deletion failed; inspect local provider diagnostics",
        }
    verification = subprocess.run(az_with_subscription([
        "az", "storage", "blob", "exists", "--account-name", artifact["account"],
        "--container-name", artifact["container"], "--name", artifact["blob"],
        "--auth-mode", "login", "--only-show-errors", "--query", "exists", "-o", "tsv",
    ], values.get("subscription")), text=True, capture_output=True)
    absent = verification.returncode == 0 and verification.stdout.strip().lower() == "false"
    return {
        "deleted": absent, "already_absent": already_absent, "verified_absent": absent,
        "returncode": verification.returncode,
        "error": "" if verification.returncode == 0 else "artifact absence check failed",
    }


def download_artifact(
    artifact: dict[str, Any], destination: Path, config: dict[str, Any]
) -> tuple[bool, str]:
    if artifact.get("provider") == "tigris":
        return download_s3_artifact(artifact, destination, _artifact_values(artifact, config))
    result = subprocess.run(az_with_subscription([
        "az", "storage", "blob", "download", "--account-name", artifact["account"],
        "--container-name", artifact["container"], "--name", artifact["blob"],
        "--file", str(destination), "--auth-mode", "login", "--only-show-errors", "-o", "none",
    ], config.get("artifact_store", {}).get("subscription")), text=True, capture_output=True)
    return result.returncode == 0, result.stderr[-2000:]
