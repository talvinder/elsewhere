"""Standalone bounded workspace runner, copied to the approved remote workspace.

Only the standard library is used. No local control-plane credentials are copied.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path


def run(spec_path: str) -> int:
    base = Path(spec_path).resolve().parent
    spec = json.loads(Path(spec_path).read_text())
    # A second invocation cannot repeat a command after uncertain dispatch.
    claim = base / "started"
    try:
        fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError:
        return 73
    work = base / "work"
    work.mkdir()
    with tarfile.open(base / "source.tar.gz", "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not member.isfile():
                raise ValueError("unsafe source archive")
            target = work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("source member missing")
            target.write_bytes(stream.read())
            target.chmod(member.mode & 0o777)
    manifest = json.loads((work / ".agent-capacity-manifest.json").read_text())
    if manifest["content_sha256"] != spec["source_fingerprint"]:
        raise ValueError("source lineage mismatch")
    for item in manifest["files"]:
        if (
            hashlib.sha256((work / item["path"]).read_bytes()).hexdigest()
            != item["sha256"]
        ):
            raise ValueError("source checksum mismatch")
    output = base / "result"
    output.mkdir()
    activity = spec["activity_commands"]
    subprocess.run(activity["start"], check=True, capture_output=True)
    child = None

    def terminate(*_):
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
        raise InterruptedError("cancelled")

    signal.signal(signal.SIGTERM, terminate)
    code = 125
    try:
        with (
            (output / "stdout.txt").open("wb") as stdout,
            (output / "stderr.txt").open("wb") as stderr,
        ):
            child = subprocess.Popen(
                ["/bin/sh", "-lc", spec["command"]],
                cwd=work,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                preexec_fn=lambda: resource.setrlimit(
                    resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024)
                ),
            )
            try:
                code = child.wait(timeout=spec["max_runtime_seconds"])
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                code = 124
            except InterruptedError:
                if child.poll() is None:
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                code = 130
    finally:
        # Stop descendants even when the shell exited successfully.
        if child:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        activity_released = (
            subprocess.run(activity["stop"], capture_output=True).returncode == 0
        )
    (output / "exit_code").write_text(str(code))
    (output / "missing.txt").write_text("")
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "job_id": spec["id"],
                "requested_paths": spec["result_paths"],
                "source_fingerprint": spec["source_fingerprint"],
                "lineage": spec["lineage"],
                "activity_released": activity_released,
                "completed_at": int(time.time()),
            }
        )
    )
    # Return the approved repository tree plus requested files for review, never links.
    files = output / "files"
    files.mkdir()
    total = 0
    exclusions = spec["exclusions"]
    for path in sorted(work.rglob("*")):
        relative = path.relative_to(work)
        requested = any(
            relative == Path(value) or Path(value) in relative.parents
            for value in spec["result_paths"]
        )
        excluded = set(exclusions["paths"]) - (
            {"dist", "build", ".next"} if requested else set()
        )
        if (
            path.is_symlink()
            or not path.is_file()
            or any(part.lower() in excluded for part in relative.parts)
        ):
            continue
        if (
            path.name.lower().startswith((".env", "service-account-"))
            or path.name.lower() in exclusions["names"]
            or path.suffix.lower() in exclusions["suffixes"]
        ):
            continue
        if path.stat().st_size > 100 * 1024 * 1024:
            raise ValueError("result file exceeds safety limit")
        content = path.read_bytes()
        total += len(content)
        if total > 200 * 1024 * 1024:
            raise ValueError("result tree exceeds safety limit")
        target = files / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    missing = [value for value in spec["result_paths"] if not (files / value).exists()]
    (output / "missing.txt").write_text("\n".join(missing))
    checks = []
    for path in sorted(output.rglob("*")):
        if path.is_file():
            checks.append(
                hashlib.sha256(path.read_bytes()).hexdigest()
                + "  "
                + path.relative_to(output).as_posix()
            )
    (output / "checksums.sha256").write_text("\n".join(checks) + "\n")
    temporary = base / "result.pending.tar.gz"
    with tarfile.open(temporary, "w:gz") as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                archive.add(
                    path, arcname=path.relative_to(output).as_posix(), recursive=False
                )
    temporary.replace(base / "result.tar.gz")
    return code


if __name__ == "__main__":
    sys.exit(run(sys.argv[1]))
