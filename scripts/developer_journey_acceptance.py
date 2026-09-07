#!/usr/bin/env python3
"""Exercise a real regression, correction, result recovery and cleanup on Fly.

The deliberately broken source exists only in a private disposable snapshot.
Default invocation is a dry plan. Cloud work requires --execute and existing trust.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_capacity import cli  # noqa: E402
from agent_capacity.journey import completion_receipt  # noqa: E402


def run_case(source: Path, expected_exit: int, execute: bool) -> dict:
    command = (
        "python -m pip install --quiet 'boto3>=1.34,<2' && "
        "PYTHONPATH=src python -m unittest discover -s tests -p test_developer_journey.py"
    )
    arguments = {
        "workload": "test", "provider": "fly", "image": "python:3.13-bookworm",
        "command": command, "source_path": str(source), "cpu": 1, "memory_mb": 1024,
        "max_runtime_seconds": 600, "estimated_cost_usd": 0.05,
        "result_paths": [".agent-capacity-manifest.json"],
    }
    planned = cli.mcp_call_tool("elsewhere_plan", arguments)
    print(json.dumps({
        "stage": "plan", "placement": planned["decision"]["placement"],
        "trust_allowed": planned["trust"]["allowed"],
        "reasons": planned["trust"]["reasons"], "estimated_cost_usd": 0.05,
        "execution": "explicit Fly lifecycle acceptance", "expected_exit": expected_exit,
    }), flush=True)
    if not execute:
        return {"dry_run": True}
    if not planned["trust"]["allowed"]:
        raise RuntimeError("existing approval does not cover acceptance")
    dispatched = cli.mcp_call_tool("elsewhere_dispatch", {
        **arguments, "approval_receipt": planned["trust"]["receipt"],
    })
    job = dispatched.get("job", {})
    job_id = job.get("id")
    if not job_id:
        raise RuntimeError("dispatch did not return a recoverable job ID")
    print(json.dumps({"stage": "submitted", "job_id": job_id}), flush=True)
    try:
        if dispatched.get("returncode"):
            raise RuntimeError("submission failed")
        cursor = ""
        for _ in range(45):
            observation = cli.wait_for_job(job_id, 30, cursor)
            cursor = observation.get("cursor", cursor)
            if observation.get("needs_attention"):
                raise RuntimeError("provider status unavailable; inspect retained job")
            if observation.get("changed"):
                print(json.dumps({"stage": "progress", "state": observation["job"]["state"]}), flush=True)
            if observation.get("terminal"):
                break
        else:
            raise RuntimeError("acceptance observation deadline reached")
        current = cli.find_job(job_id)
        result = current.get("result", {})
        if result.get("state") != "collected" or result.get("exit_code") != expected_exit:
            raise RuntimeError("unexpected test result; inspect retained logs")
        if expected_exit and "test_fingerprint_tracks_transported_changes" not in result.get("stderr", ""):
            raise RuntimeError("failure was not the deliberate source regression")
        returned = Path(result["local_path"]) / "files/.agent-capacity-manifest.json"
        manifest = json.loads(returned.read_text())
        if manifest["content_sha256"] != completion_receipt(current)["source_fingerprint"]:
            raise RuntimeError("returned source identity differs from transported source")
        if not any(item["path"] == ".env" for item in manifest["skipped"]):
            raise RuntimeError("harmless secret-exclusion marker was not excluded")
        code, _ = cli.capture_job_action(job_id, "cleanup")
        receipt = completion_receipt(cli.find_job(job_id))
        if code or not receipt["cleanup_verified"] or not returned.is_file():
            raise RuntimeError("cleanup or retained result verification failed")
        print(json.dumps({"stage": "verified", "receipt": receipt}), flush=True)
        return receipt
    except BaseException:
        current = cli.find_job(job_id) or {}
        if current.get("state") not in cli.TERMINAL_JOB_STATES:
            cli.capture_job_action(job_id, "cancel")
        cli.capture_job_action(job_id, "cleanup")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-parent", type=Path, required=True,
                        help="private directory inside an already approved source root")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.snapshot_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="journey-", dir=args.snapshot_parent) as directory:
        source = Path(directory)
        for name in ("src", "tests"):
            shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (source / ".env").write_text("ELSEWHERE_TEST_MARKER=harmless\n")
        module = source / "src/agent_capacity/journey.py"
        correct = module.read_text()
        module.write_text(correct.replace("return hashlib.sha256(json.dumps(selected, sort_keys=True, separators=(\",\", \":\")).encode()).hexdigest()", "return '0' * 64"))
        failed = run_case(source, 1, args.execute)
        module.write_text(correct)
        passed = run_case(source, 0, args.execute)
        if args.execute and failed["source_fingerprint"] == passed["source_fingerprint"]:
            raise RuntimeError("the correction must change the transported source identity")
        args.output.write_text(json.dumps({"failed_case": failed, "corrected_case": passed}, indent=2) + "\n")


if __name__ == "__main__":
    main()
