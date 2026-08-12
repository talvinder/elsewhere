#!/usr/bin/env python3
"""Capture a privacy-safe install-to-cleanup journey linked to a proven run.

The script performs only read-only checks and a non-billable dry plan. The live run,
result recovery, and cleanup steps come from a ledger-derived run record produced by
``export_public_run_evidence.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
PARTICIPANT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def _revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True
    )
    revision = completed.stdout.strip()
    return revision if completed.returncode == 0 and REVISION.fullmatch(revision) else "unknown"


def _platform() -> str:
    value = platform.system().lower()
    return "macos" if value == "darwin" else value


def _run_json(command: list[str], cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if completed.returncode:
        message = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise ValueError(message)
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"command returned invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("command did not return a JSON object")
    return value


def _validated_run(record: dict[str, Any], participant_id: str) -> dict[str, Any]:
    if record.get("participant_id") != participant_id:
        raise ValueError("run evidence belongs to a different participant")
    if record.get("capture_method") != "elsewhere-job-store-v1":
        raise ValueError("run evidence was not exported from the Elsewhere job ledger")
    if record.get("result_verified") is not True or record.get("cleanup_verified") is not True:
        raise ValueError("run evidence does not prove result recovery and cleanup")
    if record.get("scenario") != "success":
        raise ValueError("the install-to-cleanup journey must link to a successful live run")
    if record.get("provider") not in {"fly", "azure"}:
        raise ValueError("run evidence has no supported compute provider")
    for field in ("result_bundle_sha256", "job_evidence_sha256"):
        if not SHA256.fullmatch(str(record.get(field, ""))):
            raise ValueError(f"run evidence has no valid {field}")
    if not record.get("id") or not record.get("elsewhere_version"):
        raise ValueError("run evidence has no public identity or Elsewhere version")
    return record


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def capture_journey(
    participant_id: str,
    run_record: dict[str, Any],
    source_path: Path,
    elsewhere_command: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not PARTICIPANT_ID.fullmatch(participant_id):
        raise ValueError(
            "participant ID must be 1-40 lowercase letters, digits, dashes, or underscores"
        )
    run_record = _validated_run(run_record, participant_id)
    command = elsewhere_command.expanduser().resolve()
    if not command.is_file():
        raise ValueError("Elsewhere command is not an installed file")
    source = source_path.expanduser().resolve()
    if not source.is_dir():
        raise ValueError("source path must be an existing directory")

    installed = subprocess.run(
        [str(command), "--version"], cwd=source, text=True, capture_output=True
    )
    expected_version = f"elsewhere {run_record['elsewhere_version']}"
    if installed.returncode or installed.stdout.strip() != expected_version:
        raise ValueError(
            "installed Elsewhere version does not match the ledger-derived run"
        )

    doctor = _run_json(
        [str(command), "doctor", "--source-path", str(source), "--json"], source
    )
    if doctor.get("ready_for_execution") is not True:
        raise ValueError("Elsewhere doctor does not report execution readiness")

    provider = str(run_record["provider"])
    plan = _run_json([
        str(command), "route",
        "--workload", "test",
        "--execution", "remote",
        "--provider", provider,
        "--image", "curlimages/curl:8.10.1",
        "--source-path", str(source),
        "--command", "printf elsewhere-journey-dry-plan",
        "--cpu", "1",
        "--memory-mb", "512",
        "--max-runtime-seconds", "300",
        "--estimated-cost-usd", str(run_record["estimated_cost_usd"]),
    ], source)
    if plan.get("executed") is not False:
        raise ValueError("journey planning unexpectedly executed work")
    if plan.get("decision", {}).get("placement") != "remote":
        raise ValueError("dry plan did not select remote execution")
    if plan.get("job", {}).get("provider") != provider:
        raise ValueError("dry plan provider differs from the linked live run")
    if plan.get("plan", {}).get("trust", {}).get("allowed") is not True:
        raise ValueError("dry plan is outside the active trust contract")

    captured_at = now or datetime.now(UTC)
    record = {
        "participant_id": participant_id,
        "run_id": run_record["id"],
        "completed_at": captured_at.isoformat(),
        "platform": _platform(),
        "capture_method": "elsewhere-journey-v1",
        "installed_version": run_record["elsewhere_version"],
        "installed_command_sha256": _hash_file(command),
        "journey_capture_revision": _revision(),
        "dry_plan_provider": provider,
        "dry_plan_trust_allowed": True,
        "doctor_ready_for_execution": True,
        "result_bundle_sha256": run_record["result_bundle_sha256"],
        "job_evidence_sha256": run_record["job_evidence_sha256"],
        "steps": {
            "install": "command-and-version-verified",
            "doctor": "execution-ready",
            "dry_plan": "remote-trust-approved",
            "live_run": "linked-ledger-run",
            "result_recovery": "checksum-verified",
            "cleanup": "compute-and-artifacts-verified-absent",
        },
    }
    record["journey_evidence_sha256"] = hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return record


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture an install-to-cleanup journey linked to proven run evidence"
    )
    parser.add_argument("--participant-id", required=True)
    parser.add_argument("--run-evidence", type=Path, required=True)
    parser.add_argument("--source-path", type=Path, default=Path.cwd())
    parser.add_argument("--elsewhere-command", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    command = args.elsewhere_command
    if command is None:
        installed = shutil.which("elsewhere")
        if not installed:
            raise SystemExit("elsewhere is not installed on PATH")
        command = Path(installed)
    try:
        run_record = json.loads(args.run_evidence.read_text())
        record = capture_journey(
            args.participant_id, run_record, args.source_path, command
        )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise SystemExit(str(error)) from error
    encoded = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded)
        print(f"wrote verified journey evidence to {args.output}")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
