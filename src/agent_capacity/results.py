"""Provider-neutral remote result bundle creation and verification."""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

RESULT_FORMAT_VERSION = 1
MAX_RESULT_PATHS = 32
MAX_RESULT_BUNDLE_BYTES = 250 * 1024 * 1024
MAX_RESULT_EXPANDED_BYTES = 250 * 1024 * 1024
MAX_RESULT_MEMBERS = 4096
MAX_RESULT_PATH_LENGTH = 512
MAX_RESULT_MANIFEST_BYTES = 1024 * 1024
MAX_RESULT_CHECKSUM_BYTES = 4 * 1024 * 1024
MAX_RESULT_TEXT_BYTES = 16 * 1024 * 1024


def validate_result_paths(values: Iterable[str] | None) -> list[str]:
    paths: list[str] = []
    for raw in values or []:
        if not isinstance(raw, str) or not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
            raise ValueError("result paths must be non-empty single-line strings")
        if len(raw) > MAX_RESULT_PATH_LENGTH:
            raise ValueError(f"result paths may not exceed {MAX_RESULT_PATH_LENGTH} characters")
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts or raw in {".", "./"}:
            raise ValueError(f"result path must stay inside the remote workspace: {raw}")
        normalized = path.as_posix().removeprefix("./")
        if normalized not in paths:
            paths.append(normalized)
    if len(paths) > MAX_RESULT_PATHS:
        raise ValueError(f"at most {MAX_RESULT_PATHS} result paths may be requested")
    return paths


def wrap_result_command(
    command: str,
    job_id: str,
    upload_url: str,
    result_paths: Iterable[str] | None,
    max_runtime_seconds: int,
) -> str:
    """Run the workload, bundle exact results, upload them, then return its exit code."""
    paths = validate_result_paths(result_paths)
    result_dir = f"/tmp/elsewhere-result-{job_id}"
    archive = f"/tmp/elsewhere-result-{job_id}.tar.gz"
    copy_steps: list[str] = []
    for path in paths:
        destination = f"{result_dir}/files/{path}"
        parent = str(PurePosixPath(destination).parent)
        copy_steps.append(
            f"if [ -e {shlex.quote(path)} ]; then mkdir -p {shlex.quote(parent)}; "
            f"cp -R {shlex.quote(path)} {shlex.quote(destination)}; "
            f"else printf '%s\\n' {shlex.quote(path)} >> {shlex.quote(result_dir + '/missing.txt')}; fi"
        )
    manifest = json.dumps(
        {"version": RESULT_FORMAT_VERSION, "job_id": job_id, "requested_paths": paths},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "; ".join([
        "set -u",
        "command -v timeout >/dev/null 2>&1 || { echo 'Elsewhere requires timeout' >&2; exit 125; }",
        "command -v tar >/dev/null 2>&1 || { echo 'Elsewhere result upload requires tar' >&2; exit 126; }",
        "command -v sha256sum >/dev/null 2>&1 || { echo 'Elsewhere result upload requires sha256sum' >&2; exit 126; }",
        "command -v curl >/dev/null 2>&1 || { echo 'Elsewhere result upload requires curl' >&2; exit 126; }",
        f"result_dir={shlex.quote(result_dir)}",
        f"result_archive={shlex.quote(archive)}",
        'rm -rf "$result_dir" "$result_archive"',
        'mkdir -p "$result_dir/files"',
        ': > "$result_dir/missing.txt"',
        "set +e",
        f"timeout -s TERM {int(max_runtime_seconds)} /bin/sh -lc {shlex.quote(command)} "
        '>' + '"$result_dir/stdout.txt" 2>"$result_dir/stderr.txt"',
        "workload_exit=$?",
        "set -e",
        'printf "%s\\n" "$workload_exit" > "$result_dir/exit_code"',
        f"printf '%s\\n' {shlex.quote(manifest)} > \"$result_dir/manifest.json\"",
        *copy_steps,
        '(cd "$result_dir" && find . -type f ! -name checksums.sha256 '
        "-exec sha256sum '{}' ';' | sort > checksums.sha256)",
        'COPYFILE_DISABLE=1 tar -czf "$result_archive" -C "$result_dir" .',
        # The blob PUT overwrites, so it is safe to retry. --retry/--retry-delay and
        # --connect-timeout are available on every modern curl and turn a single
        # transient network blip or 5xx/429 from silently losing the result into a
        # bounded retry. Resumable/chunked upload for very large results is roadmap.
        f"curl -fsS --retry 5 --retry-delay 2 --connect-timeout 30 "
        f"-X PUT -H 'x-ms-blob-type: BlockBlob' -H 'Content-Type: application/gzip' "
        f"--data-binary @\"$result_archive\" {shlex.quote(upload_url)}",
        'exit "$workload_exit"',
    ])


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if len(members) > MAX_RESULT_MEMBERS:
        raise ValueError("result archive contains too many members")
    if sum(member.size for member in members if member.isfile()) > MAX_RESULT_EXPANDED_BYTES:
        raise ValueError("expanded result bundle exceeds the 250 MB safety limit")
    names: set[str] = set()
    for member in members:
        path = PurePosixPath(member.name)
        normalized = path.as_posix().removeprefix("./")
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe result archive path: {member.name}")
        if normalized in {"", "."}:
            if not member.isdir():
                raise ValueError(f"unsafe result archive path: {member.name}")
            normalized = "."
        if normalized in names:
            raise ValueError(f"duplicate result archive path: {normalized}")
        names.add(normalized)
        if member.issym() or member.islnk() or member.isdev():
            raise ValueError(f"unsupported result archive member: {member.name}")
    return members


def _read_limited_text(path: Path, maximum: int, label: str) -> str:
    if path.stat().st_size > maximum:
        raise ValueError(f"result {label} exceeds its safety limit")
    return path.read_text(errors="replace")


def inspect_result_bundle(bundle: Path, destination: Path) -> dict[str, Any]:
    size = bundle.stat().st_size
    if size > MAX_RESULT_BUNDLE_BYTES:
        raise ValueError("result bundle exceeds the 250 MB safety limit")
    bundle_sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest()
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle, "r:gz") as archive:
        members = _safe_members(archive)
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read result archive member: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)

    manifest = json.loads(_read_limited_text(
        destination / "manifest.json", MAX_RESULT_MANIFEST_BYTES, "manifest"
    ))
    if not isinstance(manifest, dict):
        raise ValueError("result manifest must be a JSON object")
    if manifest.get("version") != RESULT_FORMAT_VERSION:
        raise ValueError("unsupported result manifest version")
    job_id = manifest.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("result manifest has no job identity")
    requested_paths = manifest.get("requested_paths")
    if not isinstance(requested_paths, list) or any(
        not isinstance(value, str) for value in requested_paths
    ):
        raise ValueError("result manifest requested_paths must be a string array")
    if validate_result_paths(requested_paths) != requested_paths:
        raise ValueError("result manifest requested_paths are not normalized")
    checksums = _read_limited_text(
        destination / "checksums.sha256", MAX_RESULT_CHECKSUM_BYTES, "checksum list"
    ).splitlines()
    verified_files = []
    checksum_paths: set[str] = set()
    for line in checksums:
        try:
            expected, relative = line.split("  ", 1)
        except ValueError as error:
            raise ValueError("malformed result checksum entry") from error
        relative = relative.removeprefix("./")
        checksum_path = PurePosixPath(relative)
        if (
            not relative
            or relative == "."
            or checksum_path.is_absolute()
            or ".." in checksum_path.parts
        ):
            raise ValueError(f"unsafe result checksum path: {relative}")
        if relative in checksum_paths:
            raise ValueError(f"duplicate result checksum path: {relative}")
        checksum_paths.add(relative)
        target = destination / checksum_path.as_posix()
        if not target.is_file():
            raise ValueError(f"result checksum references a missing file: {relative}")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"result checksum mismatch: {relative}")
        verified_files.append(relative)
    extracted_files = set()
    for path in destination.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(destination).as_posix()
        if relative != "checksums.sha256":
            extracted_files.add(relative)
    if checksum_paths != extracted_files:
        missing_checksums = sorted(extracted_files - checksum_paths)
        unexpected_checksums = sorted(checksum_paths - extracted_files)
        details = []
        if missing_checksums:
            details.append("unverified files: " + ", ".join(missing_checksums))
        if unexpected_checksums:
            details.append("checksums without files: " + ", ".join(unexpected_checksums))
        raise ValueError("result checksum coverage is incomplete: " + "; ".join(details))
    required = {"manifest.json", "exit_code", "stdout.txt", "stderr.txt", "missing.txt"}
    if not required.issubset(verified_files):
        missing_checksums = sorted(required - set(verified_files))
        raise ValueError(
            "result checksums do not cover required metadata: " + ", ".join(missing_checksums)
            + f" (verified: {', '.join(sorted(verified_files))})"
        )
    exit_code = int(_read_limited_text(destination / "exit_code", 64, "exit code").strip())
    files_root = destination / "files"
    returned_paths = sorted(
        path.relative_to(files_root).as_posix()
        for path in files_root.rglob("*")
        if path.is_file()
    ) if files_root.exists() else []
    missing = [
        line for line in _read_limited_text(
            destination / "missing.txt", MAX_RESULT_MANIFEST_BYTES, "missing-path list"
        ).splitlines() if line
    ]
    return {
        "format_version": RESULT_FORMAT_VERSION,
        "job_id": job_id,
        "requested_paths": requested_paths,
        "returned_paths": returned_paths,
        "missing_paths": missing,
        "exit_code": exit_code,
        "stdout": _read_limited_text(destination / "stdout.txt", MAX_RESULT_TEXT_BYTES, "stdout"),
        "stderr": _read_limited_text(destination / "stderr.txt", MAX_RESULT_TEXT_BYTES, "stderr"),
        "verified_files": verified_files,
        "bundle_sha256": bundle_sha256,
        "bundle_size": size,
        "local_path": str(destination),
    }
