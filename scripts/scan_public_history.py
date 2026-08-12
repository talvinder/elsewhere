#!/usr/bin/env python3
"""Scan every locally reachable Git snapshot for public-repository hazards.

The scanner reports categories and locations, never matched secret values. Optional
known-value input checks personal provider identifiers without committing them.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT_BLOB_BYTES = 10 * 1024 * 1024
INTERNAL_NAME = re.compile(
    r"(^|/)(internal|private)/|\.internal\.|"
    r"(^|/)(BRAND|DISTRIBUTION|GTM|GO-?TO-?MARKET|LAUNCH|STRATEGY|MONETI[ZS]ATION|"
    r"PRICING|COMPETITIVE|BUSINESS[_-]?MODEL|POSITIONING)[^/]*\."
    r"(md|markdown|txt|pdf|docx?|xlsx?|pptx?)$",
    re.IGNORECASE,
)
INTERNAL_REF = re.compile(
    r"(^|/)(brand|distribution|gtm|go-to-market|strategy|moneti[zs]ation|"
    r"pricing|competitive|business-model|positioning)([-_/]|$)",
    re.IGNORECASE,
)
SENSITIVE_NAME = re.compile(
    r"(^|/)(\.env(?:\.[^/]+)?|\.npmrc|\.pypirc|\.netrc|credentials(?:\.json)?|"
    r"service-account\.json|secrets\.json|id_rsa|id_ed25519)$|"
    r"\.(pem|key|p12|pfx)$",
    re.IGNORECASE,
)
SAFE_ENV_NAMES = {".env.example", ".env.sample", ".env.template"}
INTERNAL_MARKER = b"ELSEWHERE:" + b"INTERNAL"
SECRET_PATTERNS = (
    ("private key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("GitHub token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("OpenAI API key", re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("Google API key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
)
URL = re.compile(rb"https?://[^\s\"'<>]+")
SIGNED_QUERY_KEYS = {
    "sig", "signature", "x-amz-credential", "x-amz-signature",
    "x-goog-credential", "x-goog-signature",
}
SAFE_HOST_SUFFIXES = (".example", ".example.com", ".test", ".invalid", ".localhost")
PLACEHOLDER_VALUE = re.compile(
    r"^(?:secret|supersecret|redacted|example|dummy|placeholder|test|must-not-appear|"
    r"[a-z]*secret[a-z]*|x{8,})$",
    re.IGNORECASE,
)
CREDENTIAL_ASSIGNMENT = re.compile(
    rb"(?im)\b(?:AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|AZURE_STORAGE_KEY|"
    rb"TIGRIS_STORAGE_AWS_SECRET_ACCESS_KEY|OPENAI_API_KEY|GITHUB_TOKEN)"
    rb"\s*[:=]\s*[\"']?([A-Za-z0-9/+_=.-]{12,})"
)


def git(command: list[str], root: Path = ROOT, *, check: bool = True) -> bytes:
    completed = subprocess.run(
        ["git", *command], cwd=root, capture_output=True, check=False
    )
    if check and completed.returncode:
        raise RuntimeError(
            completed.stderr.decode(errors="replace").strip()
            or f"git {' '.join(command)} failed"
        )
    return completed.stdout


def finding(category: str, location: str, commit: str = "") -> dict[str, str]:
    value = {"category": category, "location": location}
    if commit:
        value["commit"] = commit
    return value


def read_known_values(path: Path | None) -> list[tuple[str, bytes]]:
    if path is None:
        return []
    values: list[tuple[str, bytes]] = []
    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        if "=" not in raw:
            raise ValueError(f"known-value line {number} must be label=value")
        label, value = raw.split("=", 1)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", label):
            raise ValueError(f"known-value line {number} has an invalid label")
        if len(value) < 4:
            raise ValueError(f"known-value line {number} is too short to match safely")
        values.append((label, value.encode()))
    return values


def read_credentials_env(path: Path | None) -> list[tuple[str, bytes]]:
    if path is None:
        return []
    values: list[tuple[str, bytes]] = []
    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith("export "):
            raw = raw.removeprefix("export ").strip()
        if "=" not in raw:
            raise ValueError(f"credential line {number} must be NAME=value")
        name, value = raw.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", name):
            raise ValueError(f"credential line {number} has an invalid name")
        if not re.search(r"(?:ACCESS_KEY|SECRET|TOKEN|BUCKET)", name):
            continue
        value = value.strip().strip("\"'")
        if len(value) < 4:
            raise ValueError(f"credential line {number} is too short to match safely")
        values.append((name.lower().replace("_", "-"), value.encode()))
    return values


def read_provider_values(path: Path | None) -> list[tuple[str, bytes]]:
    if path is None:
        return []
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("provider config must be a JSON object")
    fields = (
        ("fly-app", ("providers", "fly", "app")),
        ("fly-org", ("providers", "fly", "org")),
        ("azure-subscription", ("providers", "azure", "subscription")),
        ("azure-resource-group", ("providers", "azure", "resource_group")),
        ("artifact-bucket", ("artifact_store", "bucket")),
        ("artifact-account", ("artifact_store", "account")),
    )
    values: list[tuple[str, bytes]] = []
    for label, keys in fields:
        current: Any = value
        for key in keys:
            current = current.get(key) if isinstance(current, dict) else None
        if isinstance(current, str) and len(current) >= 4:
            values.append((label, current.encode()))
    return values


def signed_url_present(data: bytes) -> bool:
    for raw in URL.findall(data):
        try:
            parsed = urlsplit(raw.decode("utf-8", errors="strict").rstrip(".,);]"))
        except (UnicodeDecodeError, ValueError):
            continue
        host = (parsed.hostname or "").lower()
        if host == "example.com" or host.endswith(SAFE_HOST_SUFFIXES):
            continue
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() in SIGNED_QUERY_KEYS and not PLACEHOLDER_VALUE.fullmatch(value):
                return True
    return False


def content_categories(data: bytes, known_values: Iterable[tuple[str, bytes]]) -> set[str]:
    categories: set[str] = set()
    if INTERNAL_MARKER in data:
        categories.add("internal marker")
    for category, pattern in SECRET_PATTERNS:
        if pattern.search(data):
            categories.add(category)
    for match in CREDENTIAL_ASSIGNMENT.finditer(data):
        candidate = match.group(1).decode(errors="replace")
        if not PLACEHOLDER_VALUE.fullmatch(candidate):
            categories.add("credential assignment")
            break
    if signed_url_present(data):
        categories.add("signed URL")
    for label, value in known_values:
        if value in data:
            categories.add(f"known private value ({label})")
    return categories


def scan_history(
    root: Path = ROOT,
    known_values_path: Path | None = None,
    provider_config_path: Path | None = None,
    credentials_env_path: Path | None = None,
) -> dict[str, Any]:
    known_values = read_known_values(known_values_path) + read_provider_values(
        provider_config_path
    ) + read_credentials_env(credentials_env_path)
    commits = git(["rev-list", "--all"], root).decode().splitlines()
    blob_locations: dict[str, set[tuple[str, str]]] = defaultdict(set)
    findings: dict[tuple[str, str], dict[str, str]] = {}

    def add(category: str, location: str, commit: str = "") -> None:
        key = (category, location)
        if key not in findings:
            findings[key] = finding(category, location, commit)

    refs = git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes", "refs/tags"],
        root,
    ).decode(errors="replace").splitlines()
    for ref in refs:
        if not ref.endswith("/HEAD") and INTERNAL_REF.search(ref):
            add("internal ref name", ref)

    for commit in commits:
        message = git(["show", "-s", "--format=%B", commit], root)
        for category in content_categories(message, known_values):
            add(category, "<commit message>", commit)
        tree = git(["ls-tree", "-r", "-z", commit], root)
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            _mode, kind, object_id = metadata.decode().split()
            if kind != "blob":
                continue
            path = raw_path.decode("utf-8", errors="replace")
            basename = Path(path).name.lower()
            if INTERNAL_NAME.search(path):
                add("internal filename", path, commit)
            if SENSITIVE_NAME.search(path) and basename not in SAFE_ENV_NAMES:
                add("sensitive filename", path, commit)
            blob_locations[object_id].add((commit, path))

    scanned_blobs = 0
    skipped_binary_blobs = 0
    skipped_large_blobs = 0
    for object_id, locations in blob_locations.items():
        size = int(git(["cat-file", "-s", object_id], root).decode())
        if size > MAX_TEXT_BLOB_BYTES:
            skipped_large_blobs += 1
            continue
        data = git(["cat-file", "blob", object_id], root)
        if b"\0" in data[:8192]:
            skipped_binary_blobs += 1
            continue
        scanned_blobs += 1
        for category in content_categories(data, known_values):
            commit, path = min(locations)
            add(category, path, commit)

    ordered = sorted(
        findings.values(), key=lambda value: (value["category"], value["location"])
    )
    return {
        "clean": not ordered,
        "commits_scanned": len(commits),
        "refs_scanned": len(refs),
        "text_blobs_scanned": scanned_blobs,
        "binary_blobs_skipped": skipped_binary_blobs,
        "large_blobs_skipped": skipped_large_blobs,
        "known_private_values_checked": len(known_values),
        "findings": ordered,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan all locally reachable Git history for public-repository hazards"
    )
    parser.add_argument(
        "--known-values-file",
        type=Path,
        help="private label=value file; values are matched but never printed",
    )
    parser.add_argument(
        "--provider-config",
        type=Path,
        help="private Elsewhere JSON config; provider values are matched but never printed",
    )
    parser.add_argument(
        "--credentials-env",
        type=Path,
        help="private env file; credential values are matched but never printed",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        report = scan_history(
            known_values_path=args.known_values_file,
            provider_config_path=args.provider_config,
            credentials_env_path=args.credentials_env,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"history scan failed: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        outcome = "PASS" if report["clean"] else "FAIL"
        print(
            f"{outcome}: {report['commits_scanned']} commits, "
            f"{report['text_blobs_scanned']} unique text blobs, "
            f"{report['refs_scanned']} refs"
        )
        for item in report["findings"]:
            suffix = f" at {item['commit'][:12]}" if item.get("commit") else ""
            print(f"- {item['category']}: {item['location']}{suffix}")
        if report["binary_blobs_skipped"] or report["large_blobs_skipped"]:
            print(
                "Note: "
                f"{report['binary_blobs_skipped']} binary and "
                f"{report['large_blobs_skipped']} large blobs were content-skipped; "
                "their filenames were still checked."
            )
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
