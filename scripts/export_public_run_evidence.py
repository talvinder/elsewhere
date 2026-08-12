#!/usr/bin/env python3
"""Export privacy-safe public evidence from a verified Elsewhere job.

This script never contacts a provider and never changes job state. It only accepts
jobs whose result was collected and whose compute and artifacts were verified gone.
Raw job IDs, commands, provider resource IDs, account data, and artifact locations
are intentionally excluded from the exported record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_capacity import __version__  # noqa: E402
from agent_capacity.cli import find_job  # noqa: E402

SHA256 = re.compile(r"^[0-9a-f]{64}$")
PARTICIPANT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def _revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True
    )
    revision = completed.stdout.strip()
    return revision if completed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", revision) else "unknown"


def _artifact_absence(value: dict[str, Any] | None, label: str) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"job has no cleanup evidence for {label}")
    if value.get("reason") == f"no {label}":
        return "not-used"
    if value.get("verified_absent") is not True:
        raise ValueError(f"{label} absence was not verified")
    return "verified-absent"


def build_record(job: dict[str, Any], participant_id: str, scenario: str) -> dict[str, Any]:
    if not PARTICIPANT_ID.fullmatch(participant_id):
        raise ValueError(
            "participant ID must be 1-40 lowercase letters, digits, dashes, or underscores"
        )
    if scenario not in {"success", "expected_failure"}:
        raise ValueError("scenario must be success or expected_failure")
    if job.get("provider") not in {"fly", "azure"}:
        raise ValueError("only remote Fly or Azure jobs qualify")
    if job.get("state") != "cleaned" or job.get("provider_absent") is not True:
        raise ValueError("job must be cleaned with provider absence verified")

    result = job.get("result")
    if not isinstance(result, dict) or result.get("state") != "collected":
        raise ValueError("job must have a collected result bundle")
    bundle_sha256 = str(result.get("bundle_sha256", ""))
    if not SHA256.fullmatch(bundle_sha256):
        raise ValueError("result bundle must have a verified SHA-256 checksum")
    exit_code = result.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise ValueError("result bundle must contain an integer exit code")
    if scenario == "success" and exit_code != 0:
        raise ValueError("a success scenario must have exit code 0")
    if scenario == "expected_failure" and exit_code == 0:
        raise ValueError("an expected-failure scenario must have a non-zero exit code")

    cleanup = job.get("cleanup")
    if not isinstance(cleanup, dict):
        raise ValueError("job has no cleanup evidence")
    compute = cleanup.get("compute")
    if not isinstance(compute, dict) or not (
        compute.get("verified_absent") is True or compute.get("already_absent") is True
    ):
        raise ValueError("compute absence was not verified")
    source_cleanup = _artifact_absence(cleanup.get("source_artifact"), "source artifact")
    result_cleanup = _artifact_absence(cleanup.get("result_artifact"), "result artifact")

    completed_at = job.get("cleaned_at")
    if (
        not isinstance(completed_at, int)
        or isinstance(completed_at, bool)
        or completed_at <= 0
    ):
        raise ValueError("job has no valid cleanup timestamp")
    estimated_cost = job.get("estimated_cost_usd")
    if (
        not isinstance(estimated_cost, (int, float))
        or isinstance(estimated_cost, bool)
        or not math.isfinite(float(estimated_cost))
        or estimated_cost <= 0
    ):
        raise ValueError("job must include a positive cost estimate")

    provider_config = job.get("plan", {}).get("provider_config", {})
    region = provider_config.get("region") or provider_config.get("location")
    if not isinstance(region, str) or not region.strip():
        raise ValueError("job has no provider region")
    artifact_provider = job.get("result_artifact", {}).get("provider")
    if artifact_provider not in {"tigris", "azure-blob"}:
        raise ValueError("job has no supported artifact-provider evidence")

    raw_job_id = str(job.get("id", ""))
    if not raw_job_id:
        raise ValueError("job has no identity")
    public_job_hash = hashlib.sha256(
        f"elsewhere-public-job-v1:{raw_job_id}".encode()
    ).hexdigest()
    proof = {
        "artifact_provider": artifact_provider,
        "bundle_sha256": bundle_sha256,
        "cleaned_at": completed_at,
        "compute_absent": True,
        "exit_code": exit_code,
        "job_hash": public_job_hash,
        "provider": job["provider"],
        "region": region,
        "result_artifact_cleanup": result_cleanup,
        "source_artifact_cleanup": source_cleanup,
    }
    proof_hash = hashlib.sha256(
        json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "id": f"run-{public_job_hash[:20]}",
        "participant_id": participant_id,
        "provider": job["provider"],
        "artifact_provider": artifact_provider,
        "scenario": scenario,
        "completed_at": datetime.fromtimestamp(completed_at, UTC).isoformat(),
        "region": region,
        "estimated_cost_usd": round(float(estimated_cost), 6),
        "result_verified": True,
        "cleanup_verified": True,
        "source_transport_verified": source_cleanup == "verified-absent",
        "result_bundle_sha256": bundle_sha256,
        "job_evidence_sha256": proof_hash,
        "capture_method": "elsewhere-job-store-v1",
        "evidence_exporter_revision": _revision(),
        "elsewhere_version": __version__,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export a privacy-safe public proof from a completed Elsewhere job"
    )
    parser.add_argument(
        "job_id", help="local Elsewhere job ID or name; never written to output"
    )
    parser.add_argument("--participant-id", required=True)
    parser.add_argument("--scenario", choices=("success", "expected_failure"), required=True)
    parser.add_argument("--output", type=Path, help="write one JSON record here; stdout if omitted")
    args = parser.parse_args()

    job = find_job(args.job_id)
    if job is None:
        raise SystemExit("unknown job")
    try:
        record = build_record(job, args.participant_id, args.scenario)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    encoded = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded)
        print(f"wrote verified public run evidence to {args.output}")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
