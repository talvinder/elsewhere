"""Small, provider-neutral next actions and completion receipts."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def placement_advice(workload: str) -> dict[str, Any]:
    return {
        "action": "assess_remote_placement",
        "workload": workload,
        "message": (
            "Local capacity is unavailable. Before waiting, assess whether this work "
            "can run remotely with elsewhere_plan or elsewhere route. Keep device, "
            "signing, and other host-dependent work local and explain that constraint."
        ),
        "requires": ["portable command", "source boundary", "runtime image", "matching trust receipt"],
        "automatic_dispatch": False,
    }


def source_fingerprint(files: list[dict[str, Any]]) -> str:
    """Hash exact transported content and paths, independent of archive timestamps."""
    selected = sorted(
        ({key: entry[key] for key in ("path", "sha256", "size")} for entry in files),
        key=lambda entry: entry["path"],
    )
    return hashlib.sha256(json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def completion_receipt(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result") or {}
    source = (job.get("source_artifact") or {}).get("manifest") or {}
    start = job.get("submitted_at") or job.get("started_at")
    end = job.get("completed_at")
    return {
        "job_id": job["id"],
        "provider": job.get("provider"),
        "state": job.get("state"),
        "source_fingerprint": source.get("content_sha256"),
        "source_file_count": source.get("file_count"),
        "source_skipped_count": source.get("skipped_count"),
        "exit_code": result.get("exit_code", job.get("returncode")),
        "result_verified": result.get("state") == "collected",
        "result_path": result.get("local_path"),
        "elapsed_seconds": max(0, end - start) if end is not None and start is not None else None,
        "resources": {key: job.get(key) for key in ("cpu", "memory_mb", "max_runtime_seconds")},
        "estimated_cost_usd": job.get("estimated_cost_usd"),
        "cost_kind": "estimate",
        "cleanup_verified": job.get("state") == "cleaned" and job.get("provider_absent") is True,
    }
