#!/usr/bin/env python3
"""Herdr's terminal workflow for the installed Elsewhere CLI (stdlib only)."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

PLUGIN_ID = "elsewhere.workloads"
TERMINAL = {"succeeded", "completed", "failed", "cancelled", "submission_failed", "cleaned", "cleanup_failed"}


class WorkflowError(Exception):
    pass


def say(value):
    # Terminal output and workspace names must not inject escape/control sequences.
    print("".join(c if c in "\n\t" or c.isprintable() else "?" for c in str(value)), flush=True)


def context_source(explicit=None):
    context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    value = explicit or context.get("focused_pane_cwd") or context.get("workspace_cwd")
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise WorkflowError("Choose an absolute project folder; Herdr did not supply one.")
    source = Path(value).resolve(strict=True)
    if not source.is_dir() or source == Path(source.anchor) or source == Path.home():
        raise WorkflowError("Choose a project folder, rather than a home or filesystem root.")
    if source == Path(__file__).resolve().parent:
        raise WorkflowError("Choose your project, rather than the plugin's installation folder.")
    return source


def executable():
    binary = os.environ.get("ELSEWHERE_BIN", "elsewhere")
    found = shutil.which(binary)
    if not found:
        raise WorkflowError("Elsewhere is missing. Install elsewhere-run with Python 3.11+ and put elsewhere on PATH (or set ELSEWHERE_BIN).")
    return found


def call(arguments, source, *, timeout=60):
    result = subprocess.run([executable(), *arguments], cwd=source, text=True,
                            capture_output=True, timeout=timeout)
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Do not echo provider commands, credentials, or signed transport URLs.
        raise WorkflowError("Elsewhere did not return JSON. Run elsewhere doctor in this project to check setup.") from None
    if not isinstance(value, dict):
        raise WorkflowError("Elsewhere returned an unexpected response.")
    return result.returncode, value


def route_args(spec):
    command = spec.get("command")
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise WorkflowError("Enter a command to run.")
    workload = spec.get("workload", "test")
    if workload not in {"build", "test", "light"}:
        raise WorkflowError("Choose build, test, or light.")
    execution = spec.get("execution", "auto")
    if execution not in {"auto", "local", "remote"}:
        raise WorkflowError("Choose auto, local, or remote execution.")
    seconds = int(spec.get("max_runtime_seconds", 900))
    cost = float(spec.get("estimated_cost_usd", 0))
    if seconds < 60 or not math.isfinite(cost) or cost < 0:
        raise WorkflowError("Use at least 60 seconds and a finite, nonnegative cost estimate.")
    args = ["route", "--workload", workload, "--execution", execution,
            "--command", command, "--source-path", str(spec["source"]),
            "--owner", "herdr:elsewhere", "--no-queue",
            "--max-runtime-seconds", str(seconds), "--estimated-cost-usd", str(cost)]
    for key, flag in (("image", "--image"), ("provider", "--provider"),
                      ("cpu", "--cpu"), ("memory_mb", "--memory-mb")):
        if spec.get(key) is not None:
            args += [flag, str(spec[key])]
    for path in spec.get("result_paths", []):
        args += ["--result-path", path]
    return args


def preview(spec):
    code, plan = call(route_args(spec), spec["source"])
    if code:
        raise WorkflowError("Placement planning failed. Check elsewhere doctor and the workload settings.")
    decision = plan.get("decision", {})
    placement = decision.get("placement")
    if placement not in {"local", "remote"}:
        raise WorkflowError("Elsewhere did not select a location.")
    say(f"\nProject: {spec['source']}\nCommand: {spec['command']}\nRun: {placement}\nWhy: {decision.get('reason', 'unavailable')}")
    if placement == "remote":
        job, remote = plan.get("job", {}), plan.get("plan", {})
        trust = remote.get("trust", {})
        say(f"Provider: {job.get('provider')} | Region: {trust.get('region')}\n"
            f"CPU: {job.get('cpu')} | Memory: {job.get('memory_mb')} MB | Limit: {job.get('max_runtime_seconds')}s\n"
            f"Estimated cost: ${job.get('estimated_cost_usd')} (estimate, not a billing cap)\n"
            f"Return files: {', '.join(spec.get('result_paths', [])) or 'stdout and stderr only'}")
        say("Source will be packaged at execution with Elsewhere's exclusions. Private and uncommitted files require matching trust approval.")
        if trust.get("allowed") is not True:
            say("Remote execution blocked by the existing trust boundary:")
            for reason in trust.get("reasons", []):
                say(f"  {reason}")
    else:
        say("Local execution uses this project folder and Elsewhere's capacity reservation. No cloud resources are created.")
    return plan


def state_dir(source):
    root = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not root or not Path(root).is_absolute():
        raise WorkflowError("HERDR_PLUGIN_STATE_DIR must name an absolute private state folder.")
    folder = Path(root) / hashlib.sha256(str(source).encode()).hexdigest()[:24]
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def save_record(source, record):
    folder = state_dir(source)
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=folder)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(record, output, indent=2)
            output.write("\n")
        os.replace(temporary, folder / (record["id"] + ".json"))
    finally:
        Path(temporary).unlink(missing_ok=True)


def execute(spec, plan):
    location = plan["decision"]["placement"]
    trust = plan.get("plan", {}).get("trust", {})
    if location == "remote" and trust.get("allowed") is not True:
        raise WorkflowError("Remote execution is blocked. Review elsewhere trust-status; this plugin cannot approve export.")
    # Pin the reviewed location; a new capacity sample must not silently change a
    # locally reviewed command into an upload. Elsewhere rechecks admission/trust.
    arguments = route_args({**spec, "execution": location})
    if location == "remote":
        arguments += ["--approval-receipt", trust["receipt"]]
    record = {"id": uuid.uuid4().hex, "created_at": int(time.time()),
              "source": str(spec["source"]), "placement": location, "state": "starting"}
    save_record(spec["source"], record)  # Verify durable storage before dispatch.
    say("Starting reviewed work…")
    code, value = call([*arguments, "--execute"], spec["source"], timeout=None)
    job = value.get("job") or {}
    record.update({"job_id": job.get("id"), "state": job.get("state", "completed" if value.get("executed") else "not_started"),
                   "exit_code": value.get("returncode"), "dispatch_exit_code": code})
    save_record(spec["source"], record)
    for key in ("stdout", "stderr"):
        if value.get(key):
            say(value[key])
    if record["job_id"]:
        say(f"Job: {record['job_id']}\nSaved here: {state_dir(spec['source']) / (record['id'] + '.json')}")
    else:
        say(f"State: {record['state']} | Exit code: {record['exit_code']}")
    return code, record


def inspect_record(source, record):
    job_id = record.get("job_id")
    if not job_id:
        say(f"{record['state']} | Exit code: {record.get('exit_code')}")
        return record
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise WorkflowError("Invalid job identifier in saved record.")
    code, value = call(["job-status", job_id], source)
    if code:
        raise WorkflowError(f"Status unavailable. Retained job {job_id}; retry later.")
    job = value.get("job", {})
    receipt = job.get("receipt", {})
    record.update(state=job.get("state", "unknown"), receipt=receipt)
    save_record(source, record)
    say(f"Job: {job_id} | State: {record['state']}")
    if receipt:
        say(f"Exit code: {receipt.get('exit_code')} | Results verified: {receipt.get('result_verified')}\n"
            f"Result folder: {receipt.get('result_path')}\nSource fingerprint: {receipt.get('source_fingerprint')}\n"
            f"Cleanup verified: {receipt.get('cleanup_verified')}")
    return record


def cleanup(source, record):
    record = inspect_record(source, record)
    if record.get("placement") != "remote" or record.get("state") not in TERMINAL:
        raise WorkflowError("Cleanup is available only for a finished remote job.")
    if record.get("receipt", {}).get("cleanup_verified"):
        return
    if record.get("receipt", {}).get("result_verified") is not True:
        raise WorkflowError("Recover verified results before cleanup. No resources were removed.")
    code, value = call(["job-cleanup", record["job_id"]], source, timeout=None)
    record.update(state=value.get("state", record["state"]), receipt=value.get("receipt", record.get("receipt", {})))
    save_record(source, record)
    if code or record["receipt"].get("cleanup_verified") is not True:
        raise WorkflowError("Cleanup is not verified. The job is retained for retry.")
    say("Cleanup verified. Recovered files remain in the result folder.")


def review_record(source, record):
    while True:
        inspect_record(source, record)
        if not record.get("job_id"):
            return
        choice = input("[r] refresh, [l] logs, [c] clean up finished work, [b] back: ").strip().lower()
        if choice == "b":
            return
        if choice == "l":
            _, logs = call(["job-logs", record["job_id"]], source)
            say(logs.get("stdout", ""))
            say(logs.get("stderr", ""))
        elif choice == "c" and input("Remove remote resources after verified result recovery? Type cleanup: ") == "cleanup":
            cleanup(source, record)


def console(source):
    say(f"Elsewhere · {source}\nReview where a command will run, then choose whether to start it.")
    while True:
        try:
            choice = input("\n[n] new workload, [j] saved jobs, [q] quit: ").strip().lower()
            if choice == "q":
                return 0
            if choice == "j":
                records = [json.loads(p.read_text()) for p in sorted(state_dir(source).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)]
                for index, record in enumerate(records):
                    say(f"{index + 1}. {record['placement']} | {record['state']} | {record.get('job_id') or record['id']}")
                selected = input("Job number (Enter to return): ").strip()
                if selected.isdigit() and 1 <= int(selected) <= len(records):
                    review_record(source, records[int(selected) - 1])
            elif choice == "n":
                spec = {"source": source, "command": input("Command: ").strip(),
                        "workload": input("Workload [test/build/light] (test): ").strip() or "test",
                        "execution": input("Location [auto/local/remote] (auto): ").strip() or "auto"}
                # No default container or invented estimate: environment and cost
                # need to describe the command the user actually wants to run.
                spec["image"] = input("Container image for remote work (Enter for local-only): ").strip() or None
                if not spec["image"]:
                    if spec["execution"] == "remote":
                        raise WorkflowError("Remote work needs a container image.")
                    spec["execution"] = "local"
                else:
                    spec["estimated_cost_usd"] = float(input("Estimated remote cost in USD: "))
                    spec["cpu"] = int(input("Remote CPUs (2): ") or "2")
                    spec["memory_mb"] = int(input("Remote memory MB (4096): ") or "4096")
                    spec["max_runtime_seconds"] = int(input("Remote runtime limit seconds (900): ") or "900")
                    paths = input("Files/folders to return, comma-separated (Enter for logs only): ").strip()
                    spec["result_paths"] = [p.strip() for p in paths.split(",") if p.strip()]
                plan = preview(spec)
                if input("Type run to execute this command, or Enter to leave it unstarted: ") == "run":
                    _, record = execute(spec, plan)
                    if record.get("job_id"):
                        review_record(source, record)
        except (WorkflowError, ValueError, OSError, subprocess.TimeoutExpired) as error:
            say(f"Could not continue: {error}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["open", "console", "plan", "run"])
    parser.add_argument("--source", help="Explicit absolute project directory")
    parser.add_argument("--spec", type=Path, help="JSON workload specification for plan/run")
    parser.add_argument("--execute", action="store_true", help="Explicitly authorize the reviewed run")
    args = parser.parse_args()
    try:
        if args.action == "open":
            herdr = os.environ.get("HERDR_BIN_PATH", "herdr")
            return subprocess.run([herdr, "plugin", "pane", "open", "--plugin", PLUGIN_ID,
                                   "--entrypoint", "workloads"], check=False).returncode
        source = context_source(args.source)
        if args.action == "console":
            return console(source)
        if not args.spec:
            raise WorkflowError("Provide --spec with a JSON workload specification.")
        spec = {**json.loads(args.spec.read_text()), "source": source}
        plan = preview(spec)
        if args.action == "run":
            if not args.execute:
                raise WorkflowError("Nothing started. Run requires explicit --execute.")
            return execute(spec, plan)[0]
        return 0
    except (WorkflowError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        say(f"Elsewhere: {error}")
        return 1
    except (EOFError, KeyboardInterrupt):
        say("\nPane closed. Saved remote jobs remain available; closing does not cancel or clean them up.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
