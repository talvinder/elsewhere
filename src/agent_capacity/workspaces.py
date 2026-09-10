"""Shared workspace orchestration using the existing trust receipt and job ledger."""

from __future__ import annotations

import json
import math
import time
import uuid
from pathlib import Path

from agent_capacity.artifact_transport import (
    SOURCE_EXCLUDE_NAMES,
    SOURCE_EXCLUDE_SUFFIXES,
    SOURCE_EXCLUDES,
    package_source,
    source_content_fingerprint,
)
from agent_capacity.provenance import runtime_provenance
from agent_capacity.providers.sprites import SpritesProvider
from agent_capacity.results import inspect_result_bundle, validate_result_paths
from agent_capacity.workspace_contract import (
    WorkspaceError,
    approval_boundary,
    choose_workspace_provider,
    fingerprint,
)

WORKSPACE_PROVIDERS = {"sprites": SpritesProvider}


def adapter(name: str, config: dict):
    try:
        return WORKSPACE_PROVIDERS[name](config["providers"].get(name, {}))
    except KeyError:
        raise ValueError("unsupported workspace provider") from None


def plan(cli, request: dict, args, config: dict) -> tuple[dict, dict]:
    candidates = [
        adapter(name, config)
        for name in WORKSPACE_PROVIDERS
        if args.provider in ("auto", name)
    ]
    provider = choose_workspace_provider(
        request, [item for item in candidates if item.ready()[0]]
    )
    if args.git_url or args.git_ref or args.image:
        raise ValueError(
            "workspace execution requires a local source snapshot, not an OCI image or remote Git fetch"
        )
    if not args.source_path or not Path(args.source_path).is_dir():
        raise ValueError("workspace execution requires --source-path")
    if not 60 <= args.max_runtime_seconds <= 3500:
        raise ValueError("workspace runtime must be between 60 and 3500 seconds")
    if not math.isfinite(args.estimated_cost_usd) or args.estimated_cost_usd <= 0:
        raise ValueError(
            "workspace execution requires a positive finite total execution and retention cost estimate"
        )
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "name": "ew-" + job_id,
        "provider": provider.name,
        "execution_contract": "workspace",
        "workspace": request,
        "source_path": str(Path(args.source_path).resolve()),
        "source_fingerprint": source_content_fingerprint(args.source_path),
        "command": args.workload_command,
        "workload": args.workload,
        "state": "planned",
        "created_at": int(time.time()),
        "cpu": provider.resources()["cpu"],
        "memory_mb": 0,
        "max_runtime_seconds": args.max_runtime_seconds,
        "estimated_cost_usd": args.estimated_cost_usd,
        "result_paths": validate_result_paths(args.result_path),
    }
    runtime = runtime_provenance()
    job.update(
        runtime_revision=runtime["revision"],
        runtime_dirty=runtime["dirty"],
        runtime_code_sha256=runtime["code_sha256"],
        runtime_capture_method=runtime["capture_method"],
    )
    boundary = approval_boundary(job, provider.identity(provider.values))
    result = {
        "provider": provider.name,
        "provider_config": {},
        "workspace_boundary": boundary,
        "boundary_sha256": fingerprint(boundary),
        "resources": provider.resources(),
        "retention": request["retention"],
        "ready": provider.ready()[0],
        "result_delivery": "verified workspace files; task completion is separate from retention",
    }
    result["trust"] = cli.evaluate_trust(job, result, config, args.approval_receipt)
    job["plan"] = result
    return job, result


def assert_identity(provider, job: dict) -> dict | None:
    value = provider.get(job["workspace_name"])
    if value and value["id"] != job.get("workspace_id"):
        raise ValueError("workspace name now refers to a different immutable identity")
    return value


def execute(cli, job: dict, config: dict, receipt: str | None) -> dict:
    cli.require_trust(job, job["plan"], config, receipt)
    provider = adapter(job["provider"], config)
    if not provider.ready()[0]:
        raise ValueError("workspace provider is not configured")
    request = job["workspace"]
    if request.get("derivation", "snapshot") != "snapshot":
        raise ValueError(
            "native fork execution requires a verified native lifecycle; no snapshot substitution"
        )
    provider.verify_connectors(request["connectors"])
    if source_content_fingerprint(job["source_path"]) != job["source_fingerprint"]:
        raise ValueError("source content differs from the approved fingerprint; plan again")
    name = request.get("project", {}).get("name") or job["name"]
    job.update(
        submission_phase="preparing",
        workspace_name=name,
        state="submitting",
        approval_receipt=cli.trust_receipt(config["trust"]),
    )
    # Atomic project claim: a second local caller cannot mutate the same workspace.
    with cli.locked_jobs() as (data, ledger):
        if ledger.exists():
            try:
                recorded = json.loads(ledger.read_text())
                if (
                    not isinstance(recorded, dict)
                    or not isinstance(recorded.get("jobs"), list)
                    or any(not isinstance(item, dict) for item in recorded["jobs"])
                ):
                    raise ValueError("invalid ledger schema")
            except (OSError, ValueError) as error:
                raise ValueError(
                    "workspace ownership ledger is unreadable; refusing a new claim"
                ) from error
        for existing in data["jobs"]:
            if (
                existing.get("provider") == job["provider"]
                and existing.get("workspace_name") == name
                and not existing.get("workspace_released")
            ):
                raise ValueError(
                    "workspace already has an unresolved task; recover and release it first"
                )
            if (
                request["intent"] == "project"
                and existing.get("provider") == job["provider"]
                and existing.get("workspace_id") == request["project"]["id"]
                and existing.get("workspace_name") == name
                and existing.get("workspace", {}).get("intent") != "project"
            ):
                # Exact project approval transfers lifetime ownership away from a task.
                existing["retention_state"] = "transferred-to-project"
                existing["workspace_transferred"] = True
        data["jobs"].append(job)
    source = None
    try:
        if request["intent"] == "project":
            value = provider.get(name)
            if not value or value["id"] != request["project"]["id"]:
                raise ValueError("approved project workspace identity does not match")
        else:
            if provider.get(name) is not None:
                raise ValueError("new task workspace name already exists")
            value = provider.create(name)
        job["workspace_id"] = value["id"]
        cli.update_job(job["id"], workspace_id=value["id"])
        parent = request.get("parent")
        if parent:
            actual = provider.get(parent["name"])
            if not actual or actual["id"] != parent["id"]:
                raise ValueError("parent workspace identity mismatch")
            if parent.get("checkpoint") and parent["checkpoint"] not in {
                item["id"] for item in provider.checkpoints(parent["name"])
            }:
                raise ValueError("parent checkpoint not found")
        if request["intent"] == "project":
            if provider.policy(name) != request["network_policy"]:
                raise ValueError("existing project policy differs from approval")
        else:
            provider.set_policy(name, request["network_policy"])
        if provider.privileges(name) != request.get("privilege_policy", {}):
            raise ValueError("workspace privilege policy differs from approval")
        provider.verify_connectors(request["connectors"])
        # Recheck the active authority immediately before reading/exporting source.
        cli.require_trust(job, job["plan"], cli.load_config(), receipt)
        source, manifest = package_source(job["source_path"], job["id"])
        if manifest["content_sha256"] != job["source_fingerprint"]:
            raise ValueError("source changed during packaging; no source exported")
        root = provider.task_root(job["id"])
        lineage = {
            "derivation": "repository-snapshot",
            "parent": parent,
            "source_revision": cli.run_text(
                ["git", "-C", job["source_path"], "rev-parse", "HEAD"]
            ),
            "source_fingerprint": manifest["content_sha256"],
        }
        runner_spec = {
            key: job[key]
            for key in ("id", "command", "max_runtime_seconds", "result_paths")
        }
        runner_spec["exclusions"] = {
            "paths": sorted(SOURCE_EXCLUDES),
            "names": sorted(SOURCE_EXCLUDE_NAMES),
            "suffixes": list(SOURCE_EXCLUDE_SUFFIXES),
        }
        runner_spec.update(
            source_fingerprint=manifest["content_sha256"],
            lineage=lineage,
            activity_commands=provider.activity_commands(
                job["id"], job["max_runtime_seconds"]
            ),
        )
        cli.update_job(
            job["id"],
            remote_root=root,
            lineage=lineage,
            source_manifest=manifest,
            retention_state="pending",
            retention_expires_at=int(time.time()) + job["max_runtime_seconds"] + request["retention_seconds"],
        )
        provider.write(name, root + "/source.tar.gz", source.read_bytes())
        provider.write(
            name,
            root + "/runner.py",
            Path(__file__).with_name("workspace_runner.py").read_bytes(),
        )
        provider.write(name, root + "/spec.json", json.dumps(runner_spec).encode())
        cli.update_job(job["id"], submission_phase="starting_session")
        session = provider.start(
            name,
            ["python3", root + "/runner.py", root + "/spec.json"],
            job["max_runtime_seconds"] + 30,
        )
        cli.update_job(
            job["id"],
            state="running",
            submission_phase="session_identified",
            session_id=session,
            submitted_at=int(time.time()),
        )
    except BaseException as error:
        cli.update_job(
            job["id"],
            state="submission_uncertain",
            provider_evidence={"error": cli.redact_sensitive_text(str(error))},
        )
        raise
    finally:
        if source:
            source.unlink(missing_ok=True)
    return cli.find_job(job["id"])


def action(
    cli, job: dict, action_name: str, config: dict, discard_results: bool = False
) -> dict:
    if (
        action_name in ("status", "results", "logs")
        and job.get("result", {}).get("state") == "collected"
    ):
        return job  # Verified local results remain readable after remote deletion or credential expiry.
    provider = adapter(job["provider"], config)
    # A renewed grant may authorize the same exact boundary. A changed destination
    # still fails the shared comparison against the original reviewed plan.
    cli.require_trust(job, job["plan"], config)
    if not job.get("workspace_id"):
        raise ValueError(
            "workspace identity unresolved; automatic adoption or resubmission is forbidden"
        )
    value = assert_identity(provider, job)
    if (
        action_name == "cleanup"
        and job.get("retention_state") == "deleted"
        and value is None
    ):
        return job
    if value is None:
        raise ValueError("workspace is absent; absence is not completion evidence")
    if action_name in ("status", "results", "logs"):
        if job.get("result", {}).get("state") != "collected":
            try:
                raw = provider.read(
                    job["workspace_name"], job["remote_root"] + "/result.tar.gz"
                )
            except WorkspaceError as error:
                if error.status == 404:
                    evidence = {}
                    if not job.get("session_id") and job.get("remote_root"):
                        matches = [s for s in provider.sessions(job["workspace_name"])
                                   if job["remote_root"] + "/runner.py" in s.get("command", "")]
                        if len(matches) == 1:
                            job = cli.update_job(job["id"], session_id=str(matches[0]["id"]), submission_phase="session_reconciled")
                    if action_name == "logs" and job.get("session_id"):
                        evidence = provider.observe_session(job["workspace_name"], job["session_id"])
                        cli.update_job(job["id"], provider_evidence={"unverified_session_output": evidence})
                    if action_name == "logs":
                        for channel in ("stdout", "stderr"):
                            try:
                                content = provider.read(
                                    job["workspace_name"],
                                    job["remote_root"] + "/result/" + channel + ".txt",
                                )
                                evidence[channel] = cli.redact_sensitive_text(
                                    content[-12000:].decode("utf-8", errors="replace")
                                )
                            except WorkspaceError as log_error:
                                if log_error.status != 404:
                                    raise
                        cli.update_job(
                            job["id"],
                            provider_evidence={"unverified_session_output": evidence},
                        )
                    if (
                        time.time()
                        > job.get("submitted_at", job["created_at"])
                        + job["max_runtime_seconds"]
                        + 60
                    ):
                        cli.update_job(job["id"], state="outcome_unknown")
                    return cli.find_job(job["id"])
                raise
            cache = cli.result_cache_path(job["id"])
            cache.mkdir(parents=True, exist_ok=True)
            bundle = cache / "workspace.tar.gz"
            bundle.write_bytes(raw)
            result = inspect_result_bundle(bundle, cache / "verified")
            manifest = json.loads((cache / "verified/manifest.json").read_text())
            if (
                manifest["job_id"] != job["id"]
                or manifest.get("lineage") != job["lineage"]
                or manifest.get("requested_paths") != job["result_paths"]
                or manifest.get("source_fingerprint")
                != job["lineage"]["source_fingerprint"]
            ):
                raise ValueError(
                    "returned result source lineage differs from dispatched task"
                )
            result.update(
                state="collected",
                location=str(cache / "verified"),
                activity_released=manifest.get("activity_released") is True,
            )
            cli.update_job(
                job["id"],
                result=result,
                returncode=result["exit_code"],
                state="succeeded" if result["exit_code"] == 0 else "failed",
                completed_at=manifest.get("completed_at", int(time.time())),
                recovered_at=int(time.time()),
            )
        return cli.find_job(job["id"])
    if action_name == "cancel":
        if job.get("result", {}).get("state") == "collected":
            return job
        if not job.get("session_id"):
            raise ValueError("cannot cancel an unidentified session")
        provider.cancel(job["workspace_name"], job["session_id"])
        cli.update_job(job["id"], state="cancelled", completed_at=int(time.time()))
        return cli.find_job(job["id"])
    if action_name != "cleanup":
        raise ValueError("unsupported workspace action")
    if job.get("workspace_transferred"):
        raise ValueError("workspace ownership transferred to an approved project; task deletion is forbidden")
    if job.get("result", {}).get("state") != "collected":
        never_started = job.get("submission_phase") == "preparing"
        if (
            discard_results
            and not never_started
            and job.get("state")
            not in {"cancelled", "failed", "succeeded", "outcome_unknown"}
        ):
            raise ValueError("cancel active work before discarding its results")
        if never_started and job["workspace"]["intent"] == "project":
            cli.update_job(
                job["id"],
                workspace_released=True,
                retention_state="preparation-retained",
            )
            return cli.find_job(job["id"])
        if job["workspace"]["retention"] != "delete" or not (
            never_started or discard_results
        ):
            raise ValueError(
                "recover verified results before workspace retention or deletion"
            )
        cli.update_job(
            job["id"], result={"state": "not-started" if never_started else "discarded"}
        )
    retention = job["workspace"]["retention"]
    if (
        retention != "delete"
        and job.get("retention_expires_at", float("inf")) <= time.time()
        and job["workspace"]["intent"] != "project"
    ):
        if job["plan"]["workspace_boundary"].get("retention_expiry") != "delete-task-workspace-after-recovery":
            raise ValueError("expired retention has no approved deletion action")
        retention = "delete"
    if retention != "delete" and not job["result"].get("activity_released"):
        raise ValueError(
            "task activity release is unverified; do not claim sleep or completed retention"
        )
    if retention == "delete":
        if job["workspace"]["intent"] == "project":
            raise ValueError("cannot delete project workspace")
        provider.delete(job["workspace_name"])
        if provider.get(job["workspace_name"]) is not None:
            raise ValueError("workspace deletion has not been verified")
        state = "deleted"
    elif retention == "checkpoint":
        checkpoint = job.get("retained_checkpoint")
        if not checkpoint:
            # Persist intent; uncertain checkpoint creation is not retried automatically.
            if job.get("retention_state") == "checkpointing":
                raise ValueError("checkpoint outcome uncertain; reconcile before retry")
            cli.update_job(job["id"], retention_state="checkpointing")
            checkpoint = provider.checkpoint(
                job["workspace_name"], "elsewhere-" + job["id"]
            )
            cli.update_job(job["id"], retained_checkpoint=checkpoint)
        state = "checkpointed"
    else:
        state = "retained" if retention == "keep" else "idle-pause-allowed"
    cli.update_job(job["id"], retention_state=state, workspace_released=True)
    return cli.find_job(job["id"])


def reap(cli, config: dict, execute: bool = False) -> dict:
    """Resume supervisor-owned expiry. Never discard results or delete a project."""
    with cli.locked_jobs() as (data, _):
        jobs = list(data["jobs"])
    results = []
    for job in jobs:
        if (job.get("execution_contract") != "workspace"
                or job.get("retention_state") in {"deleted", "transferred-to-project", "project-released"}
                or job.get("retention_expires_at", float("inf")) > time.time()):
            continue
        item = {"job_id": job["id"], "due": True, "executed": False}
        if execute:
            try:
                current = action(cli, job, "results", config)
                current = action(cli, current, "cleanup", config)
                if current["workspace"]["intent"] == "project":
                    current = cli.update_job(job["id"], retention_state="project-released")
                item.update(executed=True, retention_state=current["retention_state"])
            except (ValueError, RuntimeError, SystemExit) as error:
                item["blocked"] = cli.redact_sensitive_text(str(error))
        results.append(item)
    return {"executed": execute, "retention": results,
            "supervision": "runs on this device; overdue work resumes when invoked after reconnect"}
