#!/usr/bin/env python3
"""Run the v0.2 remote result-and-cleanup acceptance matrix.

Cloud mutation remains opt-in: the runner exits unless --execute is present.
The evidence file intentionally excludes provider IDs, account metadata, commands,
signed URLs, and trust receipts.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from export_public_run_evidence import build_record  # noqa: E402

from agent_capacity.cli import (  # noqa: E402
    attach_execution_artifacts,
    build_dispatch_plan,
    execute_dispatch,
    find_job,
    load_config,
    refresh_remote_job,
    run_job_action,
    trust_receipt,
)


def active_trust_receipt(config: dict) -> str:
    receipt = trust_receipt(config.get("trust", {}))
    if not receipt:
        raise SystemExit("active trust receipt required before acceptance")
    return str(receipt)


def cleanup(job_id: str) -> bool:
    with contextlib.redirect_stdout(io.StringIO()):
        return run_job_action(job_id, "cleanup") == 0


def run_once(
    provider: str,
    sequence: int,
    receipt: str,
    participant_id: str,
    source_path: Path,
) -> dict:
    label = f"{provider}-{sequence}"
    expected_stdout = f"elsewhere-{label}-stdout"
    expected_file = f"elsewhere-{label}-file"
    command = (
        "test -f pyproject.toml && mkdir -p output && "
        f"printf {expected_file} > output/result.txt && "
        f"printf {expected_stdout}"
    )
    job, plan = build_dispatch_plan(
        workload="test", provider=provider, image="curlimages/curl:8.10.1",
        command=command, cpu=1, memory_mb=512, git_url=None, git_ref=None,
        source_path=str(source_path), max_runtime_seconds=300, estimated_cost_usd=0.02,
        result_paths=["output/result.txt"],
    )
    if job["provider"] != provider:
        raise RuntimeError(f"{label}: requested provider was not selected")
    job["fallback_providers"] = []
    job, plan = attach_execution_artifacts(job, command, 300, load_config(), receipt)
    started = time.time()
    try:
        returncode, _ = execute_dispatch(job, plan, receipt)
        if returncode:
            persisted = find_job(job["id"]) or {}
            attempts = persisted.get("attempts") or []
            failure_class = attempts[-1].get("failure_class", "terminal") if attempts else "terminal"
            raise RuntimeError(f"{label}: submission failed ({failure_class})")
        print(f"{label}: submitted", flush=True)

        deadline = time.time() + 420
        current = find_job(job["id"]) or job
        while time.time() < deadline:
            current, _ = refresh_remote_job(current)
            if current.get("state") in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(5)
        else:
            raise RuntimeError(f"{label}: completion timed out")

        result = current.get("result", {})
        result_root = Path(result.get("local_path", "/nonexistent"))
        returned_file = result_root / "files" / "output" / "result.txt"
        passed = (
            current.get("state") == "succeeded"
            and result.get("state") == "collected"
            and result.get("exit_code") == 0
            and result.get("stdout") == expected_stdout
            and returned_file.is_file()
            and returned_file.read_text() == expected_file
        )
        if not passed:
            raise RuntimeError(f"{label}: exact result verification failed")
        if not cleanup(job["id"]):
            raise RuntimeError(f"{label}: cleanup verification failed")
        cleaned = find_job(job["id"])
        if cleaned is None:
            raise RuntimeError(f"{label}: cleaned job evidence is unavailable")
        evidence = build_record(cleaned, participant_id, "success")
        print(f"{label}: result verified; cleanup verified", flush=True)
        return {
            **evidence,
            "acceptance_label": label,
            "elapsed_seconds": round(time.time() - started, 1),
        }
    except BaseException:
        current = find_job(job["id"])
        if current:
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    if current.get("state") not in {"succeeded", "failed", "cancelled", "cleaned"}:
                        run_job_action(job["id"], "cancel")
                    cleanup(job["id"])
                except (SystemExit, RuntimeError, OSError):
                    pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--participant-id",
        required=True,
        help="stable anonymous participant ID included in public evidence",
    )
    parser.add_argument("--provider", action="append", choices=("fly", "azure"))
    parser.add_argument(
        "--source-path",
        type=Path,
        default=ROOT,
        help="clean Git worktree transported through the configured artifact store",
    )
    parser.add_argument("--runs-per-provider", type=int, default=4)
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/elsewhere-v02-acceptance.json")
    )
    args = parser.parse_args()
    providers = args.provider or ["fly", "azure"]
    if not args.execute:
        print(json.dumps({
            "executed": False,
            "providers": providers,
            "runs_per_provider": args.runs_per_provider,
            "max_estimated_cost_usd": round(args.runs_per_provider * len(providers) * 0.02, 2),
            "next": "rerun with --execute after reviewing this plan",
        }, indent=2))
        return 0
    if not 1 <= args.runs_per_provider <= 5:
        raise SystemExit("runs per provider must be between 1 and 5")

    config = load_config()
    receipt = active_trust_receipt(config)
    source_path = args.source_path.expanduser().resolve()
    if not (source_path / "pyproject.toml").is_file():
        raise SystemExit("acceptance source must contain pyproject.toml")
    evidence = []
    for sequence in range(1, args.runs_per_provider + 1):
        for provider in providers:
            for submission_attempt in range(1, 4):
                try:
                    value = run_once(
                        provider, sequence, receipt, args.participant_id, source_path
                    )
                    value["submission_attempts"] = submission_attempt
                    evidence.append(value)
                    break
                except RuntimeError as error:
                    if "submission failed (retryable)" not in str(error) or submission_attempt == 3:
                        raise
                    print(f"{provider}-{sequence}: transient submission failure; retrying", flush=True)
                    time.sleep(5)
    payload = {
        "format": "elsewhere-v0.2-acceptance-v2",
        "version": "0.2.0a1",
        "participant_id": args.participant_id,
        "runs": evidence,
        "passed": len(evidence),
        "estimated_cost_usd": round(sum(item["estimated_cost_usd"] for item in evidence), 2),
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
