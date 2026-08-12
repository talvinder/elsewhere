"""Deterministic provenance for the installed Elsewhere Python runtime."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

GIT_REVISION = re.compile(r"^[0-9a-f]{40}$")


def runtime_code_sha256(module_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(module_dir.rglob("*.py")):
        digest.update(path.relative_to(module_dir).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def runtime_provenance(module_dir: Path | None = None) -> dict[str, Any]:
    """Fingerprint the runtime and bind it to a clean source revision when possible."""
    module_dir = (module_dir or Path(__file__).resolve().parent).resolve()
    code_sha256 = runtime_code_sha256(module_dir)
    completed = subprocess.run(
        ["git", "-C", str(module_dir), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    package_only = {
        "revision": None,
        "dirty": None,
        "code_sha256": code_sha256,
        "capture_method": "python-package-v1",
    }
    if completed.returncode:
        return package_only
    root = Path(completed.stdout.strip()).resolve()
    if module_dir != (root / "src" / "agent_capacity").resolve():
        return package_only
    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
        text=True,
        capture_output=True,
        check=False,
    )
    revision_value = revision.stdout.strip()
    if revision.returncode or status.returncode or not GIT_REVISION.fullmatch(revision_value):
        return package_only
    return {
        "revision": revision_value,
        "dirty": bool(status.stdout.strip()),
        "code_sha256": code_sha256,
        "capture_method": "source-git-v1",
    }
