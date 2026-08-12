#!/usr/bin/env python3
"""Executable public-release gate for Elsewhere."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = ROOT / "evidence/public-readiness.json"
INTERNAL_NAME = re.compile(
    r"(^|/)(internal|private)/|\.internal\.|"
    r"(^|/)(BRAND|DISTRIBUTION|GTM|GO-?TO-?MARKET|LAUNCH|STRATEGY|MONETI[ZS]ATION|"
    r"PRICING|COMPETITIVE|BUSINESS[_-]?MODEL|POSITIONING)[^/]*\.(md|markdown|txt|pdf|docx?|xlsx?|pptx?)$",
    re.IGNORECASE,
)
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
ACTION_SHA = re.compile(r"^[0-9a-f]{40}$")
WORKFLOW_ACTION = re.compile(r"^\s*-\s+uses:\s+([^\s#]+)", re.MULTILINE)
PARTICIPANT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
SUPPORTED_EVIDENCE_PROVIDERS = {"fly": "tigris", "azure": "azure-blob"}
LIVE_PROOF_PATHS = (
    "pyproject.toml",
    "scripts/export_public_run_evidence.py",
    "scripts/v02_acceptance.py",
    "src/agent_capacity",
)
LANDING_URL = "https://talvinder.com/elsewhere/"
REPOSITORY_URL = "https://github.com/talvinder/elsewhere"
INSTALL_PROBE_URL = (
    "https://github.com/talvinder/elsewhere.git/info/refs?service=git-upload-pack"
)
DOGFOOD_URL = (
    "https://raw.githubusercontent.com/talvinder/elsewhere/main/docs/DOGFOOD.md"
)
PUBLIC_USER_AGENT = "Elsewhere-Public-Readiness/0.2"


def result(name: str, passed: bool, message: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "message": message}


def run(command: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True)


def tracked_files(root: Path = ROOT) -> list[str]:
    completed = run(["git", "ls-files"], root)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "git ls-files failed")
    return [line for line in completed.stdout.splitlines() if line and (root / line).exists()]


def check_links(root: Path, files: list[str]) -> list[str]:
    broken: list[str] = []
    for relative in files:
        if not relative.lower().endswith((".md", ".markdown")):
            continue
        path = root / relative
        for target in LINK.findall(path.read_text(errors="replace")):
            target = target.strip().split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            destination = (path.parent / target).resolve()
            if not destination.exists():
                broken.append(f"{relative} -> {target}")
    return broken


def fetch_public_url(url: str) -> tuple[int, bytes, str]:
    """Fetch a public URL without ambient GitHub or provider credentials."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": PUBLIC_USER_AGENT, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read(512_000), response.geturl()
    except urllib.error.HTTPError as error:
        return error.code, error.read(32_000), error.geturl()


def online_journey_checks() -> list[dict[str, Any]]:
    """Verify the complete anonymous path advertised to a new user."""
    targets = (
        (
            "logged-out landing page",
            LANDING_URL,
            (b"Your work doesn't need your laptop", b"github.com/talvinder/elsewhere"),
            "landing page is public and points to the canonical repository",
        ),
        (
            "logged-out repository",
            REPOSITORY_URL,
            (b"talvinder/elsewhere",),
            "repository is visible without GitHub authentication",
        ),
        (
            "anonymous install endpoint",
            INSTALL_PROBE_URL,
            (b"refs/heads/",),
            "Git smart-HTTP advertises at least one branch without credentials",
        ),
        (
            "logged-out dogfood guide",
            DOGFOOD_URL,
            (b"# Dogfood", b"elsewhere"),
            "dogfood guide is readable without GitHub authentication",
        ),
    )
    checks: list[dict[str, Any]] = []
    for name, url, required, success in targets:
        try:
            status, body, final_url = fetch_public_url(url)
            missing = [value.decode(errors="replace") for value in required if value not in body]
            passed = status == 200 and not missing
            if passed:
                message = success
            elif status != 200:
                message = f"HTTP {status} from {final_url}"
            else:
                message = f"public response is missing expected content: {', '.join(missing)}"
        except (OSError, urllib.error.URLError) as error:
            passed = False
            message = f"could not fetch {url}: {error}"
        checks.append(result(name, passed, message))
    return checks


def static_checks(root: Path = ROOT) -> list[dict[str, Any]]:
    files = tracked_files(root)
    forbidden = sorted(path for path in files if INTERNAL_NAME.search(path))
    checks = [result(
        "public content boundary",
        not forbidden,
        "no internal filenames are tracked" if not forbidden else ", ".join(forbidden),
    )]

    guard = run(["sh", "scripts/check-no-internal.sh", "--tracked"], root)
    checks.append(result(
        "content guard",
        guard.returncode == 0,
        "tracked content passes" if guard.returncode == 0 else guard.stdout.strip(),
    ))

    broken = check_links(root, files)
    checks.append(result(
        "documentation links",
        not broken,
        "all relative links resolve" if not broken else "; ".join(broken),
    ))

    workflow = (root / ".github/workflows/ci.yml").read_text()
    matrix_ready = all(value in workflow for value in (
        "macos-latest", "ubuntu-latest", '"3.11"', '"3.12"', '"3.13"'
    ))
    checks.append(result(
        "cross-platform CI declaration",
        matrix_ready,
        "macOS and Linux cover Python 3.11-3.13" if matrix_ready else "CI matrix is incomplete",
    ))

    workflow_actions = []
    unpinned_actions = []
    for path in sorted((root / ".github/workflows").glob("*.y*ml")):
        for action in WORKFLOW_ACTION.findall(path.read_text()):
            if action.startswith(("./", "docker://")):
                continue
            workflow_actions.append(action)
            revision = action.rsplit("@", 1)[-1] if "@" in action else ""
            if not ACTION_SHA.fullmatch(revision):
                unpinned_actions.append(f"{path.name}: {action}")
    checks.append(result(
        "immutable CI dependencies",
        bool(workflow_actions) and not unpinned_actions,
        (
            f"all {len(workflow_actions)} workflow actions are pinned to commit SHAs"
            if workflow_actions and not unpinned_actions
            else "; ".join(unpinned_actions) or "no workflow actions found"
        ),
    ))

    codeowners_path = root / ".github/CODEOWNERS"
    codeowners = codeowners_path.read_text() if codeowners_path.exists() else ""
    owned_boundaries = (
        "/.github/ @talvinder",
        "/scripts/check-no-internal.sh @talvinder",
        "/scripts/public_readiness.py @talvinder",
        "/src/agent_capacity/artifact_transport.py @talvinder",
    )
    missing_owners = [entry for entry in owned_boundaries if entry not in codeowners]
    checks.append(result(
        "trust-boundary ownership",
        not missing_owners,
        (
            "CI, publication, and source-transfer boundaries have declared owners"
            if not missing_owners
            else "missing CODEOWNERS entries: " + ", ".join(missing_owners)
        ),
    ))

    help_result = run([sys.executable, "src/agent_capacity/cli.py", "--help"], root)
    commands_ready = help_result.returncode == 0 and "init" in help_result.stdout and "doctor" in help_result.stdout
    checks.append(result(
        "stranger onboarding commands",
        commands_ready,
        "init and doctor are installed" if commands_ready else "init or doctor is missing",
    ))
    return checks


def validate_evidence(path: Path = EVIDENCE_PATH, now: datetime | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return [result("current dogfood evidence", False, f"missing {path.relative_to(ROOT)}")]
    try:
        evidence = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        return [result("current dogfood evidence", False, f"invalid JSON: {error}")]
    runs = evidence.get("runs", [])
    participants = evidence.get("participants", [])
    journeys = evidence.get("journeys", [])
    if not all(isinstance(value, list) for value in (runs, participants, journeys)):
        return [result("evidence schema", False, "participants, runs, and journeys must be arrays")]
    participant_ids = [
        item.get("id") for item in participants if isinstance(item, dict)
    ]
    participant_schema = bool(participants) and all(
        isinstance(item, dict)
        and bool(PARTICIPANT_ID.fullmatch(str(item.get("id", ""))))
        and item.get("platform") in {"macos", "linux"}
        and item.get("role") in {"maintainer", "external"}
        for item in participants
    ) and len(participant_ids) == len(set(participant_ids)) == len(participants)
    participant_map = {
        item.get("id"): item for item in participants
        if isinstance(item, dict) and item.get("id")
    }
    run_ids = [item.get("id") for item in runs if isinstance(item, dict)]
    job_hashes = [
        item.get("job_evidence_sha256") for item in runs if isinstance(item, dict)
    ]
    bundle_hashes = [
        item.get("result_bundle_sha256") for item in runs if isinstance(item, dict)
    ]
    run_schema = bool(runs) and all(
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and bool(item["id"])
        and item.get("participant_id") in participant_map
        and item.get("provider") in SUPPORTED_EVIDENCE_PROVIDERS
        and item.get("artifact_provider")
        == SUPPORTED_EVIDENCE_PROVIDERS[item["provider"]]
        and item.get("scenario") in {"success", "expected_failure"}
        for item in runs
    ) and all(
        len(values) == len(set(values)) == len(runs)
        for values in (run_ids, job_hashes, bundle_hashes)
    )
    evidence_schema = participant_schema and run_schema
    qualifying_runs = runs if evidence_schema else []
    run_participant_ids = {
        item["participant_id"] for item in qualifying_runs
    }
    platforms = {
        str(participant_map[participant_id].get("platform", "")).lower()
        for participant_id in run_participant_ids
    }
    providers = {item["provider"] for item in qualifying_runs}
    captured = bool(qualifying_runs) and all(
        item.get("capture_method") == "elsewhere-job-store-v1"
        and bool(SHA256.fullmatch(str(item.get("result_bundle_sha256", ""))))
        and bool(SHA256.fullmatch(str(item.get("job_evidence_sha256", ""))))
        and bool(REVISION.fullmatch(str(item.get("evidence_exporter_revision", ""))))
        and bool(item.get("elsewhere_version"))
        for item in qualifying_runs
    )
    failure_runs = [
        item for item in qualifying_runs if item.get("scenario") == "expected_failure"
    ]
    verified = bool(qualifying_runs) and all(
        item.get("result_verified") is True and item.get("cleanup_verified") is True
        for item in qualifying_runs
    )
    current = now or datetime.now(UTC)

    def timestamp(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo is not None else None

    freshness_floor = current - timedelta(days=30)
    try:
        generated_at = datetime.fromisoformat(str(evidence.get("generated_at", "")))
        generated_fresh = (
            generated_at.tzinfo is not None
            and freshness_floor <= generated_at <= current + timedelta(minutes=5)
        )
    except ValueError:
        generated_fresh = False
    runs_fresh = bool(qualifying_runs) and all(
        (completed := timestamp(item.get("completed_at"))) is not None
        and freshness_floor <= completed <= current + timedelta(minutes=5)
        for item in qualifying_runs
    )
    fly_proof = any(
        str(item.get("provider", "")).lower() == "fly"
        and str(item.get("artifact_provider", "")).lower() == "tigris"
        and bool(item.get("region"))
        and isinstance(item.get("estimated_cost_usd"), (int, float))
        and not isinstance(item.get("estimated_cost_usd"), bool)
        and math.isfinite(float(item["estimated_cost_usd"]))
        and float(item["estimated_cost_usd"]) > 0
        and item.get("result_verified") is True
        and item.get("cleanup_verified") is True
        and item.get("source_transport_verified") is True
        for item in qualifying_runs
    )
    run_map = {
        item["id"]: item for item in qualifying_runs
    }
    journey_steps = {
        "install": "command-and-version-verified",
        "doctor": "execution-ready",
        "dry_plan": "remote-trust-approved",
        "live_run": "linked-ledger-run",
        "result_recovery": "checksum-verified",
        "cleanup": "compute-and-artifacts-verified-absent",
    }

    def captured_journey(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        participant_id = item.get("participant_id")
        linked = run_map.get(item.get("run_id"))
        claimed_hash = str(item.get("journey_evidence_sha256", ""))
        unhashed = {key: value for key, value in item.items() if key != "journey_evidence_sha256"}
        actual_hash = hashlib.sha256(
            json.dumps(unhashed, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        completed = timestamp(item.get("completed_at"))
        return bool(
            participant_id in participant_map
            and participant_map[participant_id].get("role") == "external"
            and item.get("platform") == participant_map[participant_id].get("platform")
            and item.get("capture_method") == "elsewhere-journey-v1"
            and item.get("steps") == journey_steps
            and item.get("doctor_ready_for_execution") is True
            and item.get("dry_plan_trust_allowed") is True
            and completed is not None
            and freshness_floor <= completed <= current + timedelta(minutes=5)
            and linked
            and linked.get("participant_id") == participant_id
            and item.get("dry_plan_provider") == linked.get("provider")
            and item.get("installed_version") == linked.get("elsewhere_version")
            and item.get("result_bundle_sha256") == linked.get("result_bundle_sha256")
            and item.get("job_evidence_sha256") == linked.get("job_evidence_sha256")
            and SHA256.fullmatch(str(item.get("installed_command_sha256", "")))
            and REVISION.fullmatch(str(item.get("journey_capture_revision", "")))
            and SHA256.fullmatch(claimed_hash)
            and claimed_hash == actual_hash
        )

    stranger_journey = any(
        captured_journey(item)
        for item in journeys
    )
    return [
        result(
            "evidence schema",
            evidence_schema,
            "participants and unique runs match the supported evidence contract"
            if evidence_schema else "participant or run evidence is malformed, duplicated, or unsupported",
        ),
        result(
            "dogfood run count",
            len(qualifying_runs) >= 25,
            f"{len(qualifying_runs)} of 25 unique qualifying runs",
        ),
        result("dogfood participants", len(run_participant_ids) >= 3, f"{len(run_participant_ids)} of 3 participants ran jobs"),
        result("dogfood platforms", {"macos", "linux"}.issubset(platforms), f"platforms: {sorted(platforms)}"),
        result("provider neutrality proof", len(providers) >= 2, f"providers: {sorted(providers)}"),
        result("failure recovery proof", len(failure_runs) >= 3, f"{len(failure_runs)} of 3 intentional failures"),
        result("result and cleanup proof", verified, "every run verified" if verified else "one or more runs are unverified or invalid"),
        result("ledger-captured proof", captured, "every run was exported from the Elsewhere job ledger" if captured else "one or more runs lack ledger-derived checksums or revision provenance"),
        result("evidence freshness", generated_fresh and runs_fresh, "file and every run are at most 30 days old" if generated_fresh and runs_fresh else "file or run evidence is stale, future-dated, or undated"),
        result("Fly and Tigris lifecycle proof", fly_proof, "Fly compute and Tigris source/result artifacts completed transport, result recovery, and verified cleanup" if fly_proof else "no complete Fly compute plus Tigris source-and-result artifact lifecycle proof"),
        result("stranger journey", stranger_journey, "an external participant captured all six steps linked to a verified run" if stranger_journey else "no external participant has a captured install-to-cleanup journey linked to verified run evidence"),
    ]


def live_proof_revision_check(
    path: Path = EVIDENCE_PATH, root: Path = ROOT
) -> dict[str, Any]:
    """Require a full Fly/Tigris proof with no later runtime-code changes."""
    try:
        evidence = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return result("live proof code revision", False, f"evidence unavailable: {error}")

    candidates = [
        item
        for item in evidence.get("runs", [])
        if isinstance(item, dict)
        and item.get("provider") == "fly"
        and item.get("artifact_provider") == "tigris"
        and item.get("source_transport_verified") is True
        and item.get("result_verified") is True
        and item.get("cleanup_verified") is True
        and REVISION.fullmatch(str(item.get("evidence_exporter_revision", "")))
    ]
    if not candidates:
        return result(
            "live proof code revision", False, "no complete Fly/Tigris proof revision"
        )

    reasons: list[str] = []
    ordered = sorted(
        candidates,
        key=lambda value: str(value.get("completed_at", "")),
        reverse=True,
    )
    for item in ordered:
        revision = str(item["evidence_exporter_revision"])
        exists = run(["git", "cat-file", "-e", f"{revision}^{{commit}}"], root)
        if exists.returncode:
            reasons.append(f"{revision[:12]} is not a local commit")
            continue
        ancestor = run(["git", "merge-base", "--is-ancestor", revision, "HEAD"], root)
        if ancestor.returncode:
            reasons.append(f"{revision[:12]} is not an ancestor of HEAD")
            continue
        changed = run(
            ["git", "diff", "--quiet", revision, "--", *LIVE_PROOF_PATHS],
            root,
        )
        if changed.returncode == 0:
            return result(
                "live proof code revision",
                True,
                f"runtime code is unchanged since proven revision {revision[:12]}",
            )
        if changed.returncode == 1:
            reasons.append(f"runtime code changed after {revision[:12]}")
        else:
            reasons.append(f"could not compare {revision[:12]} with HEAD")

    return result("live proof code revision", False, "; ".join(reasons))


def history_check(root: Path = ROOT) -> dict[str, Any]:
    completed = run(
        [sys.executable, "scripts/scan_public_history.py", "--json"], root
    )
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        report = {}
    if completed.returncode:
        findings = report.get("findings", [])
        summary = "; ".join(
            f"{item.get('category')}: {item.get('location')}"
            for item in findings[:8]
        )
        if len(findings) > 8:
            summary += f"; plus {len(findings) - 8} more"
        return result(
            "public Git history",
            False,
            summary or completed.stderr.strip() or "history scan failed",
        )
    return result(
        "public Git history",
        bool(report.get("clean")),
        f"{report.get('commits_scanned', 0)} commits and "
        f"{report.get('text_blobs_scanned', 0)} unique text blobs are public-safe",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Elsewhere public-release readiness")
    parser.add_argument("--release", action="store_true", help="include operational evidence and full Git history")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    checks = static_checks()
    if args.release:
        checks.extend(validate_evidence())
        checks.append(live_proof_revision_check())
        checks.append(history_check())
        checks.extend(online_journey_checks())
    value = {
        "ready": all(item["passed"] for item in checks),
        "passed": sum(item["passed"] for item in checks),
        "total": len(checks),
        "checks": checks,
    }
    if args.json:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        for item in checks:
            print(f"{'PASS' if item['passed'] else 'FAIL'} {item['name']}: {item['message']}")
        print(f"\n{value['passed']}/{value['total']} checks passed")
    return 0 if value["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
