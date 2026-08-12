#!/usr/bin/env python3
"""Reject release archives that contain files outside the public product boundary."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

SDIST_ALLOWED = {
    ".gitignore",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
    "PKG-INFO",
    "README.md",
    "VERSION",
    "docs",
    "examples",
    "integrations",
    "plugins",
    "pyproject.toml",
    "scripts",
    "src",
    "tests",
}
SDIST_REQUIRED = {
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "src/agent_capacity/cli.py",
    "tests/test_cli.py",
}
WHEEL_REQUIRED_SUFFIXES = {
    "agent_capacity/cli.py",
    "agent_capacity/version.py",
    ".dist-info/METADATA",
    ".dist-info/RECORD",
}


def normalized_sdist_members(archive_path: Path) -> set[str]:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if any(member.issym() or member.islnk() for member in members):
            raise ValueError("source distribution contains a symbolic or hard link")
        roots = {
            PurePosixPath(member.name).parts[0]
            for member in members
            if PurePosixPath(member.name).parts
        }
        if len(roots) != 1:
            raise ValueError("source distribution must have exactly one root directory")
        root = roots.pop()
        return {
            PurePosixPath(*PurePosixPath(member.name).parts[1:]).as_posix()
            for member in members
            if len(PurePosixPath(member.name).parts) > 1 and member.isfile()
            and PurePosixPath(member.name).parts[0] == root
        }


def check_sdist(archive_path: Path) -> None:
    members = normalized_sdist_members(archive_path)
    unexpected = sorted({PurePosixPath(path).parts[0] for path in members} - SDIST_ALLOWED)
    missing = sorted(SDIST_REQUIRED - members)
    if unexpected:
        raise ValueError("source distribution has unexpected roots: " + ", ".join(unexpected))
    if missing:
        raise ValueError("source distribution is missing: " + ", ".join(missing))


def check_wheel(archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        members = set(archive.namelist())
    unexpected = sorted({PurePosixPath(path).parts[0] for path in members} - {"agent_capacity"}
                        - {part for part in {PurePosixPath(path).parts[0] for path in members}
                           if part.endswith(".dist-info")})
    missing = sorted(
        suffix for suffix in WHEEL_REQUIRED_SUFFIXES
        if not any(path.endswith(suffix) for path in members)
    )
    if unexpected:
        raise ValueError("wheel has unexpected roots: " + ", ".join(unexpected))
    if missing:
        raise ValueError("wheel is missing: " + ", ".join(missing))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    sdists = sorted(args.dist.glob("*.tar.gz"))
    wheels = sorted(args.dist.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        raise SystemExit("expected exactly one source distribution and one wheel")
    try:
        check_sdist(sdists[0])
        check_wheel(wheels[0])
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print("PASS: release archives contain only declared public product files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
