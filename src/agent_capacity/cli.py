#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import io
import json
import math
import os
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_capacity import __version__
from agent_capacity.artifact_transport import (
    cleanup_artifact as cleanup_source_artifact,
    download_artifact,
    prepare_result_artifact,
    prepare_source_artifact,
)
from agent_capacity.models import (
    REMOTE_TERMINAL_STATES,
    should_accept_remote_transition,
)
from agent_capacity.onboarding import (
    doctor_report,
    initial_config,
    required_init_values,
)
from agent_capacity.provenance import runtime_provenance
from agent_capacity.providers import contract_complete, get_provider
from agent_capacity.results import (
    inspect_result_bundle,
    validate_result_paths,
    wrap_result_command,
)
from agent_capacity.s3_artifacts import (
    store_identity as s3_store_identity,
    store_ready as s3_store_ready,
)

WORKLOADS = {
    "service": {"mb": 256, "hard_max": 4, "heavy": False, "bursty": False},
    "light": {"mb": 512, "hard_max": 4, "heavy": False, "bursty": False},
    "parallel-agent": {"mb": 1800, "hard_max": 2, "heavy": False, "bursty": True},
    "browser": {"mb": 1500, "hard_max": 1, "heavy": True, "bursty": True},
    "build": {"mb": 4200, "hard_max": 1, "heavy": True, "bursty": True},
    "test": {"mb": 2500, "hard_max": 2, "heavy": True, "bursty": True},
}
DEFAULT_TTL = 2700
MAX_TTL = 7200
SUPPORTED_PROVIDERS = ("fly", "azure")
SUPPORTED_ARTIFACT_STORES = ("tigris", "azure-blob")
TERMINAL_LOCAL_STATES = {"succeeded", "failed", "cancelled"}
TERMINAL_JOB_STATES = TERMINAL_LOCAL_STATES | REMOTE_TERMINAL_STATES
TRUST_VERSION = 1
MCP_PROTOCOL_VERSION = "2025-06-18"
ACTION_CAPTURE_LOCK = threading.Lock()
JOBS_THREAD_LOCK = threading.RLock()
HOST_SAMPLE_MAX_AGE_SECONDS = 35
SAMPLER_LABEL = "com.elsewhere.memory-sampler"


def run_text(command: list[str]) -> str:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def redact_value(value: Any, secret: str, replacement: str = "<redacted>") -> Any:
    """Recursively remove an ephemeral secret before persistence or presentation."""
    if isinstance(value, str):
        return value.replace(secret, replacement)
    if isinstance(value, list):
        return [redact_value(item, secret, replacement) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item, secret, replacement) for key, item in value.items()}
    return value


def redact_sensitive_text(value: str) -> str:
    value = re.sub(
        r"https://[^\s'\"]+\?(?=[^\s'\"]*(?:sig|token|se|sp|x-amz-signature)=)[^\s'\"]+",
        "<redacted-signed-url>", value, flags=re.IGNORECASE,
    )
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", "Bearer <redacted>", value)
    value = re.sub(
        r"(?i)\b(AWS_SECRET_ACCESS_KEY|AZURE_CLIENT_SECRET|GITHUB_TOKEN|OPENAI_API_KEY)"
        r"\s*[=:]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=<redacted>", value,
    )
    return value


def sanitize_persisted_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, list):
        return [sanitize_persisted_value(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_persisted_value(item) for key, item in value.items()}
    return value


def public_job_view(job: dict[str, Any]) -> dict[str, Any]:
    """Return operational job state without provider account or command internals."""
    fields = (
        "id", "name", "provider", "workload", "state", "created_at", "submitted_at",
        "started_at", "completed_at", "cleaned_at", "last_checked_at", "returncode",
        "result_paths", "result", "provider_absent", "provider_evidence", "transitions",
    )
    return sanitize_persisted_value({key: job.get(key) for key in fields if key in job})


def public_provider_result(value: dict[str, Any]) -> dict[str, Any]:
    observation = value.get("observation", {})
    return sanitize_persisted_value({
        "returncode": value.get("returncode"),
        "observation": {
            key: observation.get(key)
            for key in ("state", "absent", "evidence")
            if key in observation
        },
    })


def runtime_dir() -> Path:
    return Path("/private/tmp") / f"agent-capacity-{os.getuid()}"


def host_metrics_path() -> Path:
    override = os.environ.get("AGENT_CAPACITY_HOST_METRICS")
    return Path(override).expanduser() if override else runtime_dir() / "host-memory.json"


def parse_memory_pressure(text: str) -> tuple[int, int]:
    total_match = re.search(r"system has ([0-9]+)", text)
    level_match = re.search(r"free percentage: ([0-9]+)%", text)
    total_mb = int(total_match.group(1)) // 1048576 if total_match else 0
    level = int(level_match.group(1)) if level_match else 0
    return total_mb, level


def parse_size_mb(value: str, unit: str) -> int:
    multiplier = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[unit.upper()]
    return int(float(value) * multiplier)


def parse_swap_usage(text: str) -> dict[str, Any]:
    values: dict[str, int] = {}
    for name in ("total", "used", "free"):
        match = re.search(rf"{name} = ([0-9.]+)([KMGT])", text, re.IGNORECASE)
        if match:
            values[f"swap_{name}_mb"] = parse_size_mb(match.group(1), match.group(2))
    known = len(values) == 3
    total_mb = values.get("swap_total_mb", 0)
    used_mb = values.get("swap_used_mb", 0)
    return {
        **values,
        "swap_known": known,
        "swap_utilization_percent": round(used_mb * 100 / total_mb, 1) if known and total_mb else 0.0,
    }


def parse_vm_stat(text: str) -> dict[str, int]:
    page_match = re.search(r"page size of ([0-9]+) bytes", text)
    page_size = int(page_match.group(1)) if page_match else 4096
    names = {
        "Pageouts": "pageouts",
        "Swapins": "swapins",
        "Swapouts": "swapouts",
        "Pages occupied by compressor": "compressor_pages",
        "Pages stored in compressor": "compressed_pages",
    }
    values: dict[str, int] = {"page_size_bytes": page_size}
    for source, target in names.items():
        match = re.search(rf"^{re.escape(source)}:\s+([0-9.]+)\.?$", text, re.MULTILINE)
        if match:
            values[target] = int(float(match.group(1)))
    return values


def read_host_sample(max_age: int = HOST_SAMPLE_MAX_AGE_SECONDS) -> dict[str, Any] | None:
    path = host_metrics_path()
    try:
        value = json.loads(path.read_text())
        age = max(0.0, time.time() - float(value["sampled_at"]))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if age > max_age:
        return None
    value["sample_age_seconds"] = round(age, 1)
    value["telemetry_source"] = "host-sampler"
    return value


def collect_host_sample() -> dict[str, Any]:
    pressure_text = run_text(["memory_pressure", "-Q"])
    total_mb, level = parse_memory_pressure(pressure_text)
    swap = parse_swap_usage(run_text(["sysctl", "vm.swapusage"]))
    vm = parse_vm_stat(run_text(["vm_stat"]))
    sampled_at = time.time()
    previous: dict[str, Any] = {}
    try:
        previous = json.loads(host_metrics_path().read_text())
    except (OSError, json.JSONDecodeError):
        pass
    elapsed = sampled_at - float(previous.get("sampled_at", 0))
    rates: dict[str, float] = {}
    if 0 < elapsed <= 120:
        for name in ("pageouts", "swapins", "swapouts"):
            current = int(vm.get(name, 0))
            prior = int(previous.get(name, current))
            rates[f"{name}_per_second"] = round(max(0, current - prior) / elapsed, 2)
    return {
        "version": 1,
        "sampled_at": sampled_at,
        "total_mb": total_mb,
        "memory_level": level,
        **swap,
        **vm,
        **rates,
    }


def write_host_sample() -> dict[str, Any]:
    value = collect_host_sample()
    path = host_metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temp.chmod(0o600)
    os.replace(temp, path)
    return value


def host_platform() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unsupported"


def read_proc(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def parse_meminfo(text: str) -> dict[str, Any]:
    """Total RAM, available-headroom percentage, and swap from Linux /proc/meminfo.

    MemAvailable is the kernel's own estimate of memory obtainable without swapping,
    so it maps directly onto the macOS "free percentage" the decision logic expects.
    """
    fields: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(\w+):\s+([0-9]+)\s*kB", line)
        if match:
            fields[match.group(1)] = int(match.group(2))
    total_kb = fields.get("MemTotal", 0)
    available_kb = fields.get("MemAvailable", fields.get("MemFree", 0))
    level = int(round(available_kb * 100 / total_kb)) if total_kb else 0
    swap_known = "SwapTotal" in fields
    swap: dict[str, Any] = {"swap_known": swap_known, "swap_utilization_percent": 0.0}
    if swap_known:
        swap_total_mb = fields.get("SwapTotal", 0) // 1024
        swap_free_mb = fields.get("SwapFree", 0) // 1024
        swap_used_mb = max(0, swap_total_mb - swap_free_mb)
        swap.update({
            "swap_total_mb": swap_total_mb,
            "swap_used_mb": swap_used_mb,
            "swap_free_mb": swap_free_mb,
            "swap_utilization_percent": round(swap_used_mb * 100 / swap_total_mb, 1) if swap_total_mb else 0.0,
        })
    return {"total_mb": total_kb // 1024, "memory_level": level, **swap}


def parse_pressure_stall(text: str) -> dict[str, float]:
    """Active memory-stall percentages from Linux PSI /proc/pressure/memory.

    `full avg10` is the share of the last 10 seconds in which every non-idle task was
    stalled waiting on memory — the Linux analogue of rapid macOS page-out activity,
    and unlike a page-out counter it is already time-averaged, so no sampler is needed.
    """
    result: dict[str, float] = {}
    for kind in ("some", "full"):
        match = re.search(rf"^{kind}\s+avg10=([0-9.]+)", text, re.MULTILINE)
        if match:
            result[kind] = float(match.group(1))
    return {
        "memory_stall_percent": result.get("full", 0.0),
        "memory_some_stall_percent": result.get("some", 0.0),
    }


def macos_direct_metrics() -> dict[str, Any]:
    total_mb, level = parse_memory_pressure(run_text(["memory_pressure", "-Q"]))
    return {
        "total_mb": total_mb,
        "memory_level": level,
        **parse_swap_usage(run_text(["sysctl", "vm.swapusage"])),
        "telemetry_source": "direct",
        "sensing_available": True,
        "sample_age_seconds": 0.0,
        "pageouts_per_second": 0.0,
        "swapins_per_second": 0.0,
        "swapouts_per_second": 0.0,
        "memory_stall_percent": 0.0,
    }


def linux_direct_metrics() -> dict[str, Any]:
    meminfo = parse_meminfo(read_proc("/proc/meminfo"))
    available = meminfo["total_mb"] > 0
    return {
        **meminfo,
        **parse_pressure_stall(read_proc("/proc/pressure/memory")),
        "telemetry_source": "proc" if available else "unavailable",
        "sensing_available": available,
        "sample_age_seconds": 0.0,
        "pageouts_per_second": 0.0,
        "swapins_per_second": 0.0,
        "swapouts_per_second": 0.0,
    }


def unavailable_metrics() -> dict[str, Any]:
    """Honest placeholder when local sensing is not supported: report nothing rather
    than pretending the machine has zero RAM. Remote execution still works."""
    return {
        "total_mb": 0,
        "memory_level": 0,
        "swap_known": False,
        "swap_utilization_percent": 0.0,
        "telemetry_source": "unavailable",
        "sensing_available": False,
        "sample_age_seconds": 0.0,
        "pageouts_per_second": 0.0,
        "swapins_per_second": 0.0,
        "swapouts_per_second": 0.0,
        "memory_stall_percent": 0.0,
    }


def system_metrics() -> dict[str, Any]:
    total_override = os.environ.get("AGENT_CAPACITY_TOTAL_MB")
    level_override = os.environ.get("AGENT_CAPACITY_MEMORY_LEVEL")
    sampled = read_host_sample() if os.environ.get("AGENT_CAPACITY_HOST_METRICS") else None
    platform = host_platform()
    if sampled:
        metrics = sampled
    elif platform == "darwin":
        metrics = macos_direct_metrics()
    elif platform == "linux":
        metrics = linux_direct_metrics()
    else:
        metrics = unavailable_metrics()
    if total_override:
        metrics["total_mb"] = int(total_override)
        metrics["sensing_available"] = True
    if level_override:
        metrics["memory_level"] = int(level_override)
        metrics["sensing_available"] = True
    return metrics


def state_path() -> Path:
    override = os.environ.get("AGENT_CAPACITY_STATE")
    if override:
        return Path(override).expanduser()
    return runtime_dir() / "leases.json"


def jobs_path() -> Path:
    override = os.environ.get("AGENT_CAPACITY_JOBS")
    if override:
        return Path(override).expanduser()
    return runtime_dir() / "jobs.json"


def config_path() -> Path | None:
    override = os.environ.get("AGENT_CAPACITY_CONFIG")
    if override:
        return Path(override).expanduser()
    candidates = (
        Path.cwd() / ".elsewhere.json",
        Path.cwd() / ".agent-capacity.json",
        Path.home() / ".config/elsewhere/config.json",
        Path.home() / ".config/agent-capacity/config.json",
    )
    return next((path for path in candidates if path.exists()), None)


def default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "routing": {
            "default": "fly",
            "fallbacks": [],
            "workloads": {},
        },
        "providers": {
            "fly": {
                "enabled": True,
                "app": os.environ.get("AGENT_CAPACITY_FLY_APP", ""),
                "org": os.environ.get("AGENT_CAPACITY_FLY_ORG", ""),
                "region": "bom",
                "region_fallbacks": ["sin", "hkg"],
                "cpu_kind": "shared",
            },
            "azure": {
                "enabled": False,
                "resource_group": os.environ.get("AGENT_CAPACITY_AZURE_RESOURCE_GROUP", ""),
                "location": "centralindia",
                "subscription": os.environ.get("AGENT_CAPACITY_AZURE_SUBSCRIPTION", ""),
            },
        },
        "artifact_store": {
            "provider": os.environ.get("ELSEWHERE_ARTIFACT_STORE", "tigris"),
            "bucket": os.environ.get("BUCKET_NAME", ""),
            "endpoint": os.environ.get("AWS_ENDPOINT_URL_S3", "https://t3.storage.dev"),
            "region": os.environ.get("AWS_REGION", "auto"),
            "addressing_style": "virtual",
            "presign_ttl_minutes": 60,
            "account": os.environ.get("AGENT_CAPACITY_AZURE_STORAGE_ACCOUNT", ""),
            "container": "agent-capacity-sources",
            "subscription": os.environ.get("AGENT_CAPACITY_AZURE_SUBSCRIPTION", ""),
            "sas_ttl_minutes": 60,
        },
        "trust": {
            "version": TRUST_VERSION,
            "approved": False,
            "inherit_global": True,
            "providers": {},
            "artifact_store": {},
            "source": {
                "allowed_roots": [],
                "allow_uncommitted": False,
                "allow_private": False,
            },
            "limits": {
                "max_cpu": 0,
                "max_memory_mb": 0,
                "max_runtime_seconds": 0,
                "max_estimated_cost_usd": 0,
            },
        },
    }


def global_config_path() -> Path:
    return Path.home() / ".config" / "elsewhere" / "config.json"


def merge_trust(base: dict[str, Any], values: dict[str, Any]) -> None:
    base.update({key: value for key, value in values.items() if key not in {"source", "limits"}})
    base["source"].update(values.get("source", {}))
    base["limits"].update(values.get("limits", {}))


def load_global_trust(selected_path: Path | None) -> dict[str, Any] | None:
    path = global_config_path()
    if selected_path == path or not path.exists():
        return None
    try:
        values = json.loads(path.read_text()).get("trust")
    except (OSError, json.JSONDecodeError):
        return None
    return values if isinstance(values, dict) and values.get("approved") else None


def load_config() -> dict[str, Any]:
    path = config_path()
    if path is None:
        return default_config()
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid capacity config at {path}: {error}")
    base = default_config()
    base["routing"].update(loaded.get("routing", {}))
    for provider, values in loaded.get("providers", {}).items():
        if provider in base["providers"]:
            base["providers"][provider].update(values)
        else:
            base["providers"][provider] = values
    base["artifact_store"].update(loaded.get("artifact_store", {}))
    trust = loaded.get("trust")
    # Preserve the legacy project-config behavior: an unapproved trust block
    # inherits global approval unless the project explicitly opts out.
    if (
        not isinstance(trust, dict)
        or (not trust.get("approved") and trust.get("inherit_global", True))
    ):
        trust = load_global_trust(path)
    if isinstance(trust, dict):
        merge_trust(base["trust"], trust)
    return base


def save_config(config: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    private_config_root = Path.home() / ".config" / "elsewhere"
    if path.parent == private_config_root or private_config_root in path.parents:
        path.parent.chmod(0o700)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    temp.chmod(0o600)
    os.replace(temp, path)


def trust_receipt(policy: dict[str, Any]) -> str | None:
    if not policy.get("approved"):
        return None
    receipt_payload = {key: value for key, value in policy.items() if key != "receipt"}
    digest = hashlib.sha256(
        json.dumps(receipt_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"ew1_{digest[:32]}"


def provider_identity(provider: str, config: dict[str, Any]) -> dict[str, str]:
    values = config["providers"].get(provider, {})
    return get_provider(provider).identity(values)


def provider_regions(provider: str, config: dict[str, Any]) -> list[str]:
    values = config["providers"].get(provider, {})
    return get_provider(provider).regions(values)


def artifact_store_identity(config: dict[str, Any]) -> dict[str, str]:
    values = config.get("artifact_store", {})
    if values.get("provider") == "tigris":
        return s3_store_identity(values)
    if values.get("provider") == "azure-blob":
        return {
            key: str(values.get(key, ""))
            for key in ("provider", "account", "container", "subscription")
        }
    return {"provider": str(values.get("provider", ""))}


def artifact_store_ready(config: dict[str, Any]) -> tuple[bool, str]:
    values = config.get("artifact_store", {})
    if values.get("provider") == "tigris":
        return s3_store_ready(values)
    if values.get("provider") == "azure-blob":
        if not values.get("account"):
            return False, "artifact_store.account is required"
        if shutil.which("az") is None:
            return False, "Azure CLI is required"
        return True, "ready"
    return False, f"unsupported artifact store: {values.get('provider', '')}"


def source_state(source_path: str | None) -> dict[str, Any]:
    if not source_path:
        return {"kind": "none", "dirty": False}
    root = Path(source_path).expanduser().resolve()
    git_root = run_text(["git", "-C", str(root), "rev-parse", "--show-toplevel"])
    if not git_root:
        return {"kind": "unversioned", "dirty": True}
    dirty = bool(run_text(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"]))
    return {"kind": "git", "git_root": git_root, "dirty": dirty}


def path_is_allowed(source_path: str, allowed_roots: list[str]) -> bool:
    source = Path(source_path).expanduser().resolve()
    for value in allowed_roots:
        root = Path(value).expanduser().resolve()
        if source == root or source.is_relative_to(root):
            return True
    return False


def evaluate_trust(
    job: dict[str, Any],
    plan: dict[str, Any],
    config: dict[str, Any],
    supplied_receipt: str | None = None,
) -> dict[str, Any]:
    policy_value = config.get("trust", {})
    policy = policy_value if isinstance(policy_value, dict) else {}
    active_receipt = trust_receipt(policy)
    reasons: list[str] = []
    now = datetime.now(UTC)
    if not isinstance(policy_value, dict):
        reasons.append("trust contract is malformed")
    if not policy.get("approved") or not active_receipt:
        reasons.append("no approved trust contract is configured")
    if supplied_receipt is not None and supplied_receipt != active_receipt:
        reasons.append("approval receipt does not match the active trust contract")
    expires_at = policy.get("expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at)
            if expiry.tzinfo is None or expiry <= now:
                reasons.append("trust contract has expired")
        except (TypeError, ValueError):
            reasons.append("trust contract expiry is invalid")
    else:
        reasons.append("trust contract expiry is required")

    provider = str(plan.get("provider", ""))
    approved_providers = policy.get("providers", {})
    if not isinstance(approved_providers, dict):
        reasons.append("approved provider boundary is malformed")
        approved_providers = {}
    approved_provider_value = approved_providers.get(provider)
    approved_provider = (
        approved_provider_value if isinstance(approved_provider_value, dict) else None
    )
    if not approved_provider:
        reasons.append(f"provider {provider} is not approved")
    else:
        try:
            live_provider_identity = provider_identity(provider, config)
        except (AttributeError, KeyError, TypeError, ValueError):
            live_provider_identity = None
        if approved_provider.get("identity", {}) != live_provider_identity:
            reasons.append(f"configured {provider} account differs from the approved account")
        provider_config = plan.get("provider_config", {})
        provider_config = provider_config if isinstance(provider_config, dict) else {}
        region = provider_config.get("region") or provider_config.get("location")
        approved_regions = approved_provider.get("regions", [])
        if not isinstance(approved_regions, list):
            reasons.append(f"approved {provider} regions are malformed")
            approved_regions = []
        if region and region not in approved_regions:
            reasons.append(f"region {region} is not approved for {provider}")

    limits = policy.get("limits", {})
    if not isinstance(limits, dict):
        reasons.append("approved execution limits are malformed")
        limits = {}
    for field, limit_key, label in (
        ("cpu", "max_cpu", "CPU"),
        ("memory_mb", "max_memory_mb", "memory"),
        ("max_runtime_seconds", "max_runtime_seconds", "runtime"),
    ):
        try:
            maximum = int(limits.get(limit_key, 0) or 0)
            requested = int(job.get(field, 0) or 0)
        except (TypeError, ValueError):
            maximum = 0
            requested = 0
        if maximum <= 0 or requested > maximum:
            reasons.append(f"requested {label} exceeds the approved limit")
    try:
        cost_limit = float(limits.get("max_estimated_cost_usd", 0) or 0)
        estimated_cost = float(job.get("estimated_cost_usd", 0) or 0)
    except (TypeError, ValueError):
        cost_limit = 0
        estimated_cost = 0
    if (
        not math.isfinite(cost_limit) or not math.isfinite(estimated_cost)
        or cost_limit <= 0 or estimated_cost <= 0
    ):
        reasons.append("a positive estimated cost and approved cost ceiling are required")
    elif estimated_cost > cost_limit:
        reasons.append("estimated cost exceeds the approved per-job ceiling")

    source = job.get("source_path")
    source_info = source_state(source)
    if source:
        source_policy = policy.get("source", {})
        if not isinstance(source_policy, dict):
            reasons.append("approved source boundary is malformed")
            source_policy = {}
        if not source_policy.get("allow_private"):
            reasons.append("private local source export is not approved")
        allowed_roots = source_policy.get("allowed_roots", [])
        if not isinstance(allowed_roots, list):
            reasons.append("approved source roots are malformed")
            allowed_roots = []
        if not path_is_allowed(source, allowed_roots):
            reasons.append("source path is outside the approved roots")
        if source_info.get("dirty") and not source_policy.get("allow_uncommitted"):
            reasons.append("uncommitted or unversioned source export is not approved")
    approved_store = policy.get("artifact_store", {})
    try:
        live_store = artifact_store_identity(config)
    except (AttributeError, KeyError, TypeError, ValueError):
        live_store = None
    if not isinstance(approved_store, dict):
        reasons.append("approved artifact-store boundary is malformed")
    elif approved_store != live_store:
        reasons.append("artifact-store destination differs from the approved destination")

    return {
        "allowed": not reasons,
        "receipt": active_receipt,
        "approved_at": policy.get("approved_at"),
        "expires_at": expires_at,
        "provider": provider,
        "provider_identity": live_provider_identity if approved_provider else None,
        "region": region if approved_provider else None,
        "source": str(Path(source).expanduser().resolve()) if source else None,
        "source_state": source_info,
        "estimated_cost_usd": estimated_cost,
        "reasons": reasons,
    }


def require_trust(
    job: dict[str, Any],
    plan: dict[str, Any],
    config: dict[str, Any],
    supplied_receipt: str | None = None,
) -> dict[str, Any]:
    decision = evaluate_trust(job, plan, config, supplied_receipt)
    if not decision["allowed"]:
        raise SystemExit("remote execution denied by trust contract: " + "; ".join(decision["reasons"]))
    return decision


def trust_status(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    policy_value = config.get("trust", {})
    policy = policy_value if isinstance(policy_value, dict) else {}
    expires_at = policy.get("expires_at")
    reasons: list[str] = []
    if not isinstance(policy_value, dict):
        reasons.append("trust contract is malformed")
    if not policy.get("approved"):
        reasons.append("no approved trust contract is configured")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at)
            if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                reasons.append("trust contract has expired")
        except (TypeError, ValueError):
            reasons.append("trust contract expiry is invalid")
    else:
        reasons.append("trust contract expiry is required")
    approved_providers = policy.get("providers", {})
    if not isinstance(approved_providers, dict):
        reasons.append("approved provider boundary is malformed")
        approved_providers = {}
    for provider, approved_value in approved_providers.items():
        if not isinstance(approved_value, dict):
            reasons.append(f"approved {provider} boundary is malformed")
            continue
        approved = approved_value
        try:
            live_identity = provider_identity(provider, config)
            live_regions = provider_regions(provider, config)
        except (AttributeError, KeyError, TypeError, ValueError):
            live_identity = None
            live_regions = []
        if approved.get("identity", {}) != live_identity:
            reasons.append(f"configured {provider} account differs from the approved account")
        approved_regions = approved.get("regions", [])
        if not isinstance(approved_regions, list) or not set(live_regions).issubset(
            set(approved_regions)
        ):
            reasons.append(f"configured {provider} regions differ from the approved regions")
    routing = config.get("routing", {})
    default_provider = routing.get("default") if isinstance(routing, dict) else None
    if default_provider not in approved_providers:
        reasons.append(f"default provider {default_provider} is not approved")
    try:
        live_artifact_store = artifact_store_identity(config)
    except (AttributeError, KeyError, TypeError, ValueError):
        live_artifact_store = None
    approved_artifact_store = policy.get("artifact_store", {})
    if not isinstance(approved_artifact_store, dict):
        reasons.append("approved artifact-store boundary is malformed")
    elif approved_artifact_store != live_artifact_store:
        reasons.append("artifact-store destination differs from the approved destination")
    return {
        "configured": bool(policy.get("approved")),
        "valid": not reasons,
        "reasons": reasons,
        "receipt": trust_receipt(policy),
        "approved_at": policy.get("approved_at"),
        "expires_at": expires_at,
        "approved_by": policy.get("approved_by"),
        "providers": policy.get("providers", {}),
        "artifact_store": policy.get("artifact_store", {}),
        "source": policy.get("source", {}),
        "limits": policy.get("limits", {}),
    }


def approve_trust(
    path: Path,
    providers: list[str],
    source_roots: list[str],
    allow_uncommitted: bool,
    allow_private: bool,
    max_cpu: int,
    max_memory_mb: int,
    max_runtime_seconds: int,
    max_estimated_cost_usd: float,
    expires_days: int,
) -> dict[str, Any]:
    selected = config_path()
    if path.exists():
        try:
            persisted = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise SystemExit(f"invalid capacity config at {path}: {error}")
    elif selected and selected.exists():
        persisted = json.loads(selected.read_text())
    else:
        persisted = default_config()

    config = default_config()
    config["routing"].update(persisted.get("routing", {}))
    for provider, values in persisted.get("providers", {}).items():
        config["providers"].setdefault(provider, {}).update(values)
    config["artifact_store"].update(persisted.get("artifact_store", {}))

    approved_providers = {}
    for provider in providers:
        ready, reason = provider_ready(provider, config)
        if not ready:
            raise SystemExit(f"cannot approve {provider}: {reason}")
        if provider == "azure" and not config["providers"][provider].get("subscription"):
            raise SystemExit("cannot approve azure without an explicit subscription")
        approved_providers[provider] = {
            "identity": provider_identity(provider, config),
            "regions": [region for region in provider_regions(provider, config) if region],
        }
    now = datetime.now(UTC)
    config["trust"] = {
        "version": TRUST_VERSION,
        "approved": True,
        "inherit_global": False,
        "approval_nonce": uuid.uuid4().hex,
        "approved_at": now.isoformat(),
        "expires_at": (now + timedelta(days=expires_days)).isoformat(),
        "approved_by": "local-user",
        "providers": approved_providers,
        "artifact_store": artifact_store_identity(config),
        "source": {
            "allowed_roots": [str(Path(value).expanduser().resolve()) for value in source_roots],
            "allow_uncommitted": allow_uncommitted,
            "allow_private": allow_private,
        },
        "limits": {
            "max_cpu": max_cpu,
            "max_memory_mb": max_memory_mb,
            "max_runtime_seconds": max_runtime_seconds,
            "max_estimated_cost_usd": max_estimated_cost_usd,
        },
    }
    save_config(config, path)
    return {"saved_to": str(path), **trust_status(config)}


def revoke_trust(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"revoked": False, "reason": "config does not exist", "path": str(path)}
    config = json.loads(path.read_text())
    config["trust"] = default_config()["trust"]
    config["trust"]["inherit_global"] = False
    save_config(config, path)
    return {"revoked": True, "path": str(path)}


@contextmanager
def locked_jobs() -> Iterator[tuple[dict[str, Any], Path]]:
    path = jobs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    lock_path = path.with_suffix(".lock")
    with JOBS_THREAD_LOCK:
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                data = json.loads(path.read_text()) if path.exists() else {"version": 1, "jobs": []}
            except (json.JSONDecodeError, OSError):
                data = {"version": 1, "jobs": []}
            yield data, path
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            os.replace(temp, path)


def local_job_log_path(job_id: str) -> Path:
    directory = jobs_path().parent / "job-logs"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory / f"{job_id}.log"


def append_job(job: dict[str, Any]) -> None:
    with locked_jobs() as (data, _):
        data["jobs"].append(job)


def update_job(job_id: str, **changes: Any) -> dict[str, Any] | None:
    with locked_jobs() as (data, _):
        for job in data["jobs"]:
            if job.get("id") == job_id:
                old_state = job.get("state")
                new_state = changes.get("state")
                if (
                    new_state and new_state != old_state
                    and not should_accept_remote_transition(old_state, new_state)
                ):
                    changes = {key: value for key, value in changes.items() if key != "state"}
                    new_state = None
                if new_state and new_state != old_state:
                    changed_at = int(time.time())
                    transition = {
                        "from": old_state,
                        "to": new_state,
                        "at": changed_at,
                        "evidence": sanitize_persisted_value(
                            changes.get("provider_evidence")
                            or changes.get("cleanup")
                            or changes.get("cancel_verification")
                            or {}
                        ),
                    }
                    changes["transitions"] = [*job.get("transitions", []), transition]
                    changes["state_changed_at"] = changed_at
                job.update(changes)
                return dict(job)
    return None


def spawn_local_worker(job_id: str) -> int:
    log_path = local_job_log_path(job_id)
    with log_path.open("a") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_local-worker", job_id],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    update_job(job_id, worker_pid=process.pid, log_path=str(log_path))
    return process.pid


def enqueue_local_job(
    workload: str,
    count: int,
    owner: str,
    ttl: int,
    command: list[str],
    poll_seconds: float,
    admission: dict[str, Any],
) -> dict[str, Any]:
    now = int(time.time())
    job = {
        "id": uuid.uuid4().hex,
        "name": make_job_name(workload),
        "provider": "local",
        "workload": workload,
        "count": count,
        "owner": owner,
        "ttl": ttl,
        "command": command,
        "poll_seconds": poll_seconds,
        "created_at": now,
        "state": "waiting_for_capacity",
        "last_admission": admission,
    }
    append_job(job)
    worker_pid = spawn_local_worker(job["id"])
    job["worker_pid"] = worker_pid
    job["log_path"] = str(local_job_log_path(job["id"]))
    return job


def run_local_worker(job_id: str) -> int:
    lease_token = None
    process = None
    try:
        while True:
            job = find_job(job_id)
            if job is None:
                return 1
            if job.get("cancel_requested") or job.get("state") == "cancelled":
                update_job(job_id, state="cancelled", completed_at=int(time.time()))
                return 0

            code, admission = acquire(
                job["workload"], int(job.get("count", 1)), job["owner"],
                int(job.get("ttl", DEFAULT_TTL)), owner_pid=os.getpid()
            )
            if code == 0:
                lease_token = admission["token"]
                break
            update_job(job_id, last_admission=admission, checked_at=int(time.time()))
            time.sleep(float(job.get("poll_seconds", 5.0)))

        log_path = local_job_log_path(job_id)
        update_job(
            job_id,
            state="running",
            lease_token=lease_token,
            started_at=int(time.time()),
            log_path=str(log_path),
        )
        with log_path.open("a") as log:
            process = subprocess.Popen(
                job["command"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
            update_job(job_id, process_pid=process.pid)
            with keep_lease_alive(lease_token, int(job.get("ttl", DEFAULT_TTL))):
                returncode = process.wait()

        latest = find_job(job_id) or {}
        cancelled = bool(latest.get("cancel_requested"))
        state = "cancelled" if cancelled else "succeeded" if returncode == 0 else "failed"
        update_job(
            job_id,
            state=state,
            returncode=returncode,
            completed_at=int(time.time()),
            lease_token=None,
        )
        return 0 if cancelled or returncode == 0 else returncode
    except Exception as error:
        update_job(
            job_id,
            state="failed",
            error=f"{type(error).__name__}: {error}",
            completed_at=int(time.time()),
            lease_token=None,
        )
        return 1
    finally:
        if lease_token:
            release(lease_token)


def result_cache_path(job_id: str) -> Path:
    path = jobs_path().parent / "job-results" / job_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    return path


def collect_result_artifact(job: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    artifact = job.get("result_artifact")
    if not artifact:
        return False, {"state": "unavailable", "reason": "job has no result artifact"}
    existing = job.get("result") or {}
    existing_path = Path(existing.get("local_path", "")) if existing.get("local_path") else None
    if existing.get("state") == "collected" and existing_path and existing_path.exists():
        return True, existing
    config = load_config()
    destination = result_cache_path(job["id"])
    bundle = destination.with_suffix(".tar.gz")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    downloaded, error = download_artifact(artifact, bundle, config)
    returncode = 0 if downloaded else 1
    if returncode:
        evidence = {
            "state": "pending", "returncode": returncode,
            "error": redact_sensitive_text(error),
        }
        update_job(job["id"], result=evidence)
        return False, evidence
    try:
        inspected = inspect_result_bundle(bundle, destination)
        if inspected.get("job_id") != job["id"]:
            raise ValueError("result bundle job identity does not match")
        if inspected.get("requested_paths") != artifact.get("requested_paths", []):
            raise ValueError("result bundle request manifest does not match the job")
    except (OSError, ValueError, KeyError, json.JSONDecodeError, tarfile.TarError) as error:
        evidence = {"state": "invalid", "error": f"{type(error).__name__}: {error}"}
        update_job(job["id"], result=evidence)
        return False, evidence
    finally:
        bundle.unlink(missing_ok=True)
    collected = {
        **inspected,
        "state": "collected",
        "collected_at": int(time.time()),
        "expires_at": artifact.get("expires_at"),
        "stdout": redact_sensitive_text(inspected["stdout"][-12000:]),
        "stderr": redact_sensitive_text(inspected["stderr"][-12000:]),
    }
    state = "succeeded" if inspected["exit_code"] == 0 else "failed"
    update_job(
        job["id"], result=collected, state=state, returncode=inspected["exit_code"],
        completed_at=job.get("completed_at") or int(time.time()),
    )
    return True, collected


@contextmanager
def locked_state() -> Iterator[tuple[dict[str, Any], Path]]:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text()) if path.exists() else {"version": 1, "leases": []}
        except (json.JSONDecodeError, OSError):
            data = {"version": 1, "leases": []}
        now = int(time.time())
        data["leases"] = [lease for lease in data.get("leases", []) if int(lease.get("expires_at", 0)) > now]
        yield data, path
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(temp, path)


def active_counts(leases: list[dict[str, Any]]) -> tuple[dict[str, int], int, int]:
    counts = {name: 0 for name in WORKLOADS}
    total_units = 0
    heavy_units = 0
    for lease in leases:
        workload = lease.get("workload")
        count = int(lease.get("count", 0))
        if workload not in WORKLOADS:
            continue
        counts[workload] += count
        total_units += count
        if WORKLOADS[workload]["heavy"]:
            heavy_units += count
    return counts, total_units, heavy_units


def lease_commitment(lease: dict[str, Any], now: int | None = None) -> int:
    created_at = int(lease.get("created_at", 0))
    age = max(0, (now or int(time.time())) - created_at) if created_at > 0 else 0
    reserved_mb = int(lease.get("reserved_mb", 0))
    if age <= 90:
        factor = 1.0
    elif age <= 300:
        factor = 0.5
    else:
        factor = 0.2
    return int(reserved_mb * factor)


def capacity_band(metrics: dict[str, Any]) -> dict[str, Any]:
    level = int(metrics.get("memory_level", 0))
    swapouts = float(metrics.get("swapouts_per_second", 0))
    pageouts = float(metrics.get("pageouts_per_second", 0))
    churn = max(swapouts, pageouts)
    page_size = int(metrics.get("page_size_bytes", 4096))
    activity_mb = churn * page_size / 1048576
    # Linux PSI: share of the last 10s spent fully stalled on memory. Defaults to 0.0,
    # so macOS metrics (which never set it) keep their exact swap-activity behavior.
    stall = float(metrics.get("memory_stall_percent", 0.0))
    if level < 18 or activity_mb >= 16 or stall >= 20:
        name, reason = "critical", "memory headroom is critical or the system is under heavy memory pressure"
    elif level < 30 or activity_mb >= 2 or stall >= 5:
        name, reason = "constrained", "memory is tight or memory pressure is elevated"
    elif level < 50 or activity_mb >= 0.5 or stall >= 1:
        name, reason = "guarded", "capacity is usable for quiet work but bursty work needs more margin"
    else:
        name, reason = "healthy", "the machine has room for normal local work"
    return {
        "name": name,
        "reason": reason,
        "swap_activity_per_second": round(churn, 2),
        "swap_activity_mb_per_second": round(activity_mb, 2),
        "memory_stall_percent": round(stall, 2),
    }


def available_budget(metrics: dict[str, Any], leases: list[dict[str, Any]]) -> dict[str, int]:
    total_mb = metrics["total_mb"]
    level = int(metrics["memory_level"])
    if level >= 60:
        reserve_mb = 3072
    elif level >= 45:
        reserve_mb = 3584
    elif level >= 35:
        reserve_mb = 4608
    elif level >= 25:
        reserve_mb = 5120
    else:
        reserve_mb = 6144
    reserve_mb = min(reserve_mb, max(0, total_mb - 1024))
    headroom_mb = int(total_mb * metrics["memory_level"] / 100)
    leased_mb = sum(int(lease.get("reserved_mb", 0)) for lease in leases)
    effective_leased_mb = sum(lease_commitment(lease) for lease in leases)
    swap_pct = float(metrics.get("swap_utilization_percent", 0)) if metrics.get("swap_known") else 0
    # Retained swap is historical, not proof of pressure. Keep a small margin for
    # it, but let current headroom and page-out activity drive admission.
    if swap_pct >= 90:
        swap_penalty_mb = 512
    elif swap_pct >= 75:
        swap_penalty_mb = 256
    elif swap_pct >= 50:
        swap_penalty_mb = 128
    else:
        swap_penalty_mb = 0
    activity_mb = capacity_band(metrics)["swap_activity_mb_per_second"]
    if activity_mb >= 2:
        swap_penalty_mb += 1024
    elif activity_mb >= 0.5:
        swap_penalty_mb += 512
    elif activity_mb >= 0.1:
        swap_penalty_mb += 128
    return {
        "reserve_mb": reserve_mb,
        "headroom_mb": headroom_mb,
        "leased_mb": leased_mb,
        "effective_leased_mb": effective_leased_mb,
        "swap_penalty_mb": swap_penalty_mb,
        "available_mb": max(0, headroom_mb - reserve_mb - effective_leased_mb - swap_penalty_mb),
    }


def recommend_count(workload: str, maximum: int, metrics: dict[str, Any], leases: list[dict[str, Any]]) -> int:
    definition = WORKLOADS[workload]
    band = capacity_band(metrics)["name"]
    if band == "critical":
        return 0
    if band == "constrained" and workload not in {"service", "light"}:
        return 0
    if band == "guarded" and definition["bursty"]:
        if int(metrics["memory_level"]) < 45:
            return 0

    counts, total_units, heavy_units = active_counts(leases)
    machine_unit_max = 6 if metrics["total_mb"] <= 24576 else 8
    if band == "constrained":
        machine_unit_max = 4

    workload_room = int(definition["hard_max"]) - counts[workload]
    unit_room = machine_unit_max - total_units
    heavy_room = 2 - heavy_units if definition["heavy"] else maximum
    memory_room = available_budget(metrics, leases)["available_mb"] // int(definition["mb"])
    return max(0, min(maximum, workload_room, unit_room, heavy_room, memory_room))


def payload(metrics: dict[str, Any], leases: list[dict[str, Any]]) -> dict[str, Any]:
    budget = available_budget(metrics, leases)
    return {
        "memory": metrics,
        "capacity_band": capacity_band(metrics),
        "budget": budget,
        "active_leases": leases,
        "recommendations": {
            workload: recommend_count(workload, 8, metrics, leases) for workload in WORKLOADS
        },
    }


def print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def admission_rule(workload: str, metrics: dict[str, Any], leases: list[dict[str, Any]]) -> str:
    band = capacity_band(metrics)["name"]
    if band == "critical":
        return "critical_memory_pressure"
    if band == "constrained" and workload not in {"service", "light"}:
        return "active_swap_or_low_headroom"
    if band == "guarded" and WORKLOADS[workload]["bursty"] and int(metrics["memory_level"]) < 45:
        return "insufficient_burst_headroom"
    if available_budget(metrics, leases)["available_mb"] < int(WORKLOADS[workload]["mb"]):
        return "insufficient_memory_budget"
    return "concurrency_limit"


def workload_pressure(leases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = int(time.time())
    rows = [{
        "category": str(lease.get("workload", "unknown")),
        "memory_mb": int(lease.get("reserved_mb", 0)),
        "age_seconds": max(0, now - int(lease.get("created_at", now))),
    } for lease in leases]
    return sorted(rows, key=lambda row: row["memory_mb"], reverse=True)[:5]


def acquire(
    workload: str, count: int, owner: str, ttl: int, owner_pid: int | None = None
) -> tuple[int, dict[str, Any]]:
    with locked_state() as (data, _):
        leases = data["leases"]
        metrics = system_metrics()
        recommended = recommend_count(workload, count, metrics, leases)
        if recommended < count:
            band = capacity_band(metrics)
            return 2, {
                "allowed": False,
                "requested_count": count,
                "recommended_count": recommended,
                "reason": f"{band['name']} local capacity: {band['reason']}",
                "denied_by": admission_rule(workload, metrics, leases),
                "next_action": "run `elsewhere cleanup --stale`, then retry; use `elsewhere queue` to inspect reservations",
                "memory_consumers": workload_pressure(leases),
                "privacy": "process arguments and environment values are hidden",
                **payload(metrics, leases),
            }
        token = uuid.uuid4().hex
        now = int(time.time())
        lease = {
            "token": token,
            "owner": owner,
            "workload": workload,
            "count": count,
            "reserved_mb": int(WORKLOADS[workload]["mb"]) * count,
            "created_at": now,
            "expires_at": now + ttl,
            "owner_pid": owner_pid if owner_pid is not None else os.getppid(),
        }
        leases.append(lease)
        return 0, {
            "allowed": True,
            "token": token,
            "lease": lease,
            "release_command": f"elsewhere release {token}",
            **payload(metrics, leases),
        }


def release(token: str) -> tuple[int, dict[str, Any]]:
    with locked_state() as (data, _):
        before = len(data["leases"])
        data["leases"] = [lease for lease in data["leases"] if lease.get("token") != token]
        released = len(data["leases"]) < before
        return (0 if released else 1), {"released": released, "token": token}


def renew(token: str, ttl: int) -> tuple[int, dict[str, Any]]:
    with locked_state() as (data, _):
        for lease in data["leases"]:
            if lease.get("token") == token:
                lease["expires_at"] = int(time.time()) + ttl
                return 0, {"renewed": True, "lease": lease}
        return 1, {"renewed": False, "token": token}


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def cleanup_stale() -> dict[str, Any]:
    now = int(time.time())
    cleaned_jobs: list[dict[str, Any]] = []
    released_tokens: list[str] = []
    with locked_jobs() as (job_data, _):
        for job in job_data.get("jobs", []):
            if job.get("provider") != "local" or job.get("state") in TERMINAL_LOCAL_STATES:
                continue
            worker_pid = int(job.get("worker_pid", 0))
            process_pid = int(job.get("process_pid", 0))
            if pid_alive(worker_pid) or pid_alive(process_pid):
                continue
            job["state"] = "failed"
            job["error"] = "stale local job: managed worker is no longer running"
            job["completed_at"] = now
            if job.get("lease_token"):
                released_tokens.append(str(job["lease_token"]))
                job["lease_token"] = None
            cleaned_jobs.append({
                "id": job.get("id"), "workload": job.get("workload"),
                "age_seconds": max(0, now - int(job.get("created_at", now))),
                "memory_mb": int(WORKLOADS.get(str(job.get("workload")), {}).get("mb", 0))
                * int(job.get("count", 1)),
            })

    with locked_state() as (lease_data, _):
        kept = []
        for lease in lease_data.get("leases", []):
            pid = int(lease.get("owner_pid", 0))
            stale = str(lease.get("token")) in released_tokens or (pid > 0 and not pid_alive(pid))
            if stale:
                released_tokens.append(str(lease.get("token")))
            else:
                kept.append(lease)
        lease_data["leases"] = kept

    return {
        "cleaned": bool(cleaned_jobs or released_tokens),
        "jobs": cleaned_jobs,
        "released_reservations": sorted(set(released_tokens)),
        "privacy": "process arguments and environment values are hidden",
        "capacity": payload(system_metrics(), kept),
    }


@contextmanager
def keep_lease_alive(token: str, ttl: int) -> Iterator[None]:
    override = os.environ.get("AGENT_CAPACITY_RENEW_INTERVAL_SECONDS")
    interval = float(override) if override else max(30.0, min(float(ttl) / 3, 300.0))
    stop = threading.Event()

    def renew_loop() -> None:
        while not stop.wait(interval):
            code, _ = renew(token, ttl)
            if code:
                return

    thread = threading.Thread(target=renew_loop, name="elsewhere-lease-renewal", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)


def provider_order(config: dict[str, Any], workload: str, requested: str) -> list[str]:
    if requested != "auto":
        return [requested]
    routing = config["routing"]
    preferred = routing.get("workloads", {}).get(workload, routing.get("default", "fly"))
    ordered = [preferred, *routing.get("fallbacks", [])]
    return list(dict.fromkeys(provider for provider in ordered if provider in config["providers"]))


def provider_ready(provider: str, config: dict[str, Any]) -> tuple[bool, str]:
    values = config["providers"].get(provider, {})
    adapter = get_provider(provider)
    if not contract_complete(adapter):
        return False, "provider adapter does not satisfy the lifecycle contract"
    return adapter.ready(values)


def remote_command(
    command: str,
    git_url: str | None,
    git_ref: str | None,
    source_url: str | None = None,
    max_runtime_seconds: int = 3600,
    result_url: str | None = None,
    result_paths: list[str] | None = None,
    job_id: str | None = None,
) -> str:
    bounded_command = wrap_result_command(
        command, job_id or "planned", result_url, result_paths, max_runtime_seconds
    ) if result_url else (
        "command -v timeout >/dev/null 2>&1 || "
        "{ echo 'Elsewhere requires timeout to enforce the approved runtime limit' >&2; exit 125; }; "
        f"exec timeout -s TERM {int(max_runtime_seconds)} /bin/sh -lc {shlex.quote(command)}"
    )
    if source_url:
        quoted_url = shlex.quote(source_url)
        lifecycle = (
            "set -eu; rm -rf /tmp/elsewhere-workspace; mkdir -p /tmp/elsewhere-workspace; "
            f"if command -v curl >/dev/null 2>&1; then curl -fsSL {quoted_url} -o /tmp/source.tar.gz; "
            f"else wget -qO /tmp/source.tar.gz {quoted_url}; fi; "
            "tar -xzf /tmp/source.tar.gz -C /tmp/elsewhere-workspace; cd /tmp/elsewhere-workspace; "
            f"{bounded_command}"
        )
    elif not git_url:
        lifecycle = (
            "set -eu; rm -rf /tmp/elsewhere-workspace; mkdir -p /tmp/elsewhere-workspace; "
            f"cd /tmp/elsewhere-workspace; {bounded_command}"
        )
    else:
        ref = git_ref or "main"
        lifecycle = (
            "set -eu; rm -rf /tmp/elsewhere-workspace; "
            f"git clone --depth 1 --branch {shlex.quote(ref)} {shlex.quote(git_url)} "
            f"/tmp/elsewhere-workspace; cd /tmp/elsewhere-workspace; {bounded_command}"
        )
    return (
        "command -v timeout >/dev/null 2>&1 || "
        "{ echo 'Elsewhere requires timeout to enforce the approved runtime limit' >&2; exit 125; }; "
        f"exec timeout -s TERM {int(max_runtime_seconds)} /bin/sh -lc {shlex.quote(lifecycle)}"
    )


def make_job_name(workload: str) -> str:
    safe = re.sub(r"[^a-z0-9-]", "-", workload.lower()).strip("-")[:16]
    return f"ac-{safe}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def build_fly_plan(job: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    return get_provider("fly").build_plan(job, values)


def build_azure_plan(job: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    return get_provider("azure").build_plan(job, values)


def build_dispatch_plan(
    workload: str,
    provider: str,
    image: str,
    command: str,
    cpu: int,
    memory_mb: int,
    git_url: str | None,
    git_ref: str | None,
    source_path: str | None,
    max_runtime_seconds: int = 3600,
    estimated_cost_usd: float = 0,
    result_paths: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_config()
    selected = None
    rejected: list[dict[str, str]] = []
    ordered_providers = provider_order(config, workload, provider)
    for candidate in ordered_providers:
        ready, reason = provider_ready(candidate, config)
        if ready:
            selected = candidate
            break
        rejected.append({"provider": candidate, "reason": reason})
    if selected is None:
        raise SystemExit(f"no provider is ready: {json.dumps(rejected)}")

    normalized_result_paths = validate_result_paths(result_paths)
    runtime = runtime_provenance()
    job = {
        "id": uuid.uuid4().hex,
        "name": make_job_name(workload),
        "provider": selected,
        "workload": workload,
        "image": image,
        "command": command,
        "remote_command": remote_command(command, git_url, git_ref, max_runtime_seconds=max_runtime_seconds),
        "cpu": cpu,
        "memory_mb": memory_mb,
        "git_url": git_url,
        "git_ref": git_ref,
        "source_path": str(Path(source_path).expanduser().resolve()) if source_path else None,
        "max_runtime_seconds": max_runtime_seconds,
        "estimated_cost_usd": estimated_cost_usd,
        "result_paths": normalized_result_paths,
        "runtime_revision": runtime["revision"],
        "runtime_dirty": runtime["dirty"],
        "runtime_code_sha256": runtime["code_sha256"],
        "runtime_capture_method": runtime["capture_method"],
        "created_at": int(time.time()),
        "state": "planned",
        "fallback_providers": [candidate for candidate in ordered_providers if candidate != selected],
    }
    values = config["providers"][selected]
    plan = build_fly_plan(job, values) if selected == "fly" else build_azure_plan(job, values)
    plan["rejected_providers"] = rejected
    plan["result_delivery"] = {
        "format": "elsewhere-result-v1",
        "requested_paths": normalized_result_paths,
        "transport": "approved artifact store",
        "prepared_on_execute": True,
    }
    plan["shell_preview"] = shlex.join(plan["command"])
    plan["trust"] = evaluate_trust(job, plan, config)
    return job, plan


def local_route_decision(workload: str, count: int = 1) -> dict[str, Any]:
    with locked_state() as (data, _):
        metrics = system_metrics()
        recommended = recommend_count(workload, count, metrics, data["leases"])
        sensing_available = bool(metrics.get("sensing_available", True))
        if not sensing_available:
            reason = (
                "local capacity sensing is unavailable on this platform; Elsewhere cannot "
                "measure local headroom here — run with --execution remote"
            )
        elif recommended >= count:
            reason = "local headroom and shared concurrency policy allow this workload"
        else:
            reason = "local headroom or shared concurrency policy cannot safely admit this workload"
        return {
            "placement": "local" if recommended >= count else "remote",
            "requested_count": count,
            "recommended_local_count": recommended,
            "sensing_available": sensing_available,
            "reason": reason,
            **payload(metrics, data["leases"]),
        }


def attach_execution_artifacts(
    job: dict[str, Any],
    command: str,
    max_runtime_seconds: int,
    config: dict[str, Any],
    supplied_receipt: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_artifact = None
    result_artifact = None
    try:
        if job.get("source_path"):
            source_artifact = prepare_source_artifact(job, config)
            job["source_artifact"] = source_artifact
        result_artifact = prepare_result_artifact(job, config, job.get("result_paths", []))
        job["result_artifact"] = result_artifact
        job["remote_command"] = remote_command(
            command,
            None if source_artifact else job.get("git_url"),
            None if source_artifact else job.get("git_ref"),
            source_artifact.get("url") if source_artifact else None,
            max_runtime_seconds,
            result_artifact["url"],
            job.get("result_paths", []),
            job["id"],
        )
        values = config["providers"][job["provider"]]
        plan = build_fly_plan(job, values) if job["provider"] == "fly" else build_azure_plan(job, values)
        plan["result_delivery"] = {
            "format": "elsewhere-result-v1",
            "requested_paths": job.get("result_paths", []),
            "transport": "approved artifact store",
            "prepared_on_execute": True,
        }
        plan["shell_preview"] = shlex.join(plan["command"])
        plan["trust"] = require_trust(job, plan, config, supplied_receipt)
        return job, plan
    except BaseException as preparation_error:
        rollback_failures = []
        if source_artifact:
            cleanup = cleanup_source_artifact(source_artifact, config)
            if not cleanup.get("verified_absent"):
                rollback_failures.append("source artifact")
            job.pop("source_artifact", None)
        if result_artifact:
            cleanup = cleanup_source_artifact(result_artifact, config)
            if not cleanup.get("verified_absent"):
                rollback_failures.append("result artifact")
            job.pop("result_artifact", None)
        if rollback_failures:
            raise RuntimeError(
                "artifact preparation failed and rollback could not be verified for: "
                + ", ".join(rollback_failures)
            ) from preparation_error
        raise


def execute_dispatch(
    job: dict[str, Any],
    plan: dict[str, Any],
    supplied_receipt: str | None = None,
) -> tuple[int, dict[str, Any]]:
    config = load_config()
    candidate_plans = [plan]
    if plan["provider"] == "fly":
        for region in plan["provider_config"].get("region_fallbacks", []):
            values = dict(config["providers"]["fly"])
            values["region"] = region
            candidate_plans.append(build_fly_plan(job, values))
    for provider in job.get("fallback_providers", []):
        ready, _ = provider_ready(provider, config)
        if not ready:
            continue
        values = config["providers"][provider]
        candidate_plans.append(build_fly_plan(job, values) if provider == "fly" else build_azure_plan(job, values))

    attempts = []
    result = None
    final_plan = plan
    reconciled_observation = None
    for candidate in candidate_plans:
        trust = evaluate_trust(job, candidate, config, supplied_receipt)
        if not trust["allowed"]:
            attempts.append({
                "provider": candidate["provider"],
                "provider_config": candidate["provider_config"],
                "skipped": True,
                "reason": "outside trust contract",
                "trust": trust,
            })
            continue
        result = subprocess.run(candidate["command"], text=True, capture_output=True)
        attempt = {
            "provider": candidate["provider"],
            "provider_config": candidate["provider_config"],
            "returncode": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        }
        attempts.append(attempt)
        final_plan = candidate
        if result.returncode == 0:
            break
        adapter = get_provider(candidate["provider"])
        failure_class = adapter.classify_failure(result.stderr)
        attempt["failure_class"] = failure_class
        if failure_class != "retryable":
            break
        probe_job = {**job, "provider": candidate["provider"], "plan": candidate}
        status_result = subprocess.run(adapter.status_command(probe_job), text=True, capture_output=True)
        observation = adapter.parse_status(
            status_result.stdout, status_result.stderr, status_result.returncode, probe_job
        )
        attempt["reconciliation"] = {
            "returncode": status_result.returncode,
            "absent": observation.absent,
            "state": observation.state,
            "evidence": observation.evidence,
        }
        if observation.absent:
            continue
        # A failed submission response is ambiguous until the provider proves absence.
        # Track the possible resource instead of launching a duplicate elsewhere.
        reconciled_observation = observation
        break

    if result is None:
        raise SystemExit("no execution destination is permitted by the active trust contract")
    job["provider"] = final_plan["provider"]
    if result.returncode == 0:
        job["state"] = "submitted"
    elif reconciled_observation is not None:
        job["state"] = reconciled_observation.state or "submission_uncertain"
    else:
        job["state"] = "submission_failed"
    job["submitted_at"] = int(time.time())
    job["plan"] = final_plan
    job["attempts"] = attempts
    job["submission"] = {
        "returncode": result.returncode,
        "stdout": result.stdout[-8000:],
        "stderr": result.stderr[-8000:],
    }
    job["transitions"] = [{
        "from": "planned", "to": job["state"], "at": job["submitted_at"],
        "evidence": {"provider": job["provider"], "returncode": result.returncode},
    }]
    job["state_changed_at"] = job["submitted_at"]
    if result.returncode == 0:
        job["provider_id"] = get_provider(job["provider"]).parse_submission(
            result.stdout, result.stderr, job
        )
    elif reconciled_observation is not None and reconciled_observation.provider_id:
        job["provider_id"] = reconciled_observation.provider_id
    if reconciled_observation is not None:
        job["submission_reconciled"] = True
    if job["state"] == "submission_failed":
        rollback = {}
        for artifact_name in ("source_artifact", "result_artifact"):
            artifact = job.get(artifact_name)
            if artifact:
                rollback[artifact_name] = cleanup_source_artifact(artifact, config)
        if rollback:
            job["submission_cleanup"] = rollback
    for artifact_name, replacement in (
        ("source_artifact", "<redacted-source-url>"),
        ("result_artifact", "<redacted-result-url>"),
    ):
        if job.get(artifact_name):
            signed_url = job[artifact_name].pop("url", None)
            if signed_url:
                job = redact_value(job, signed_url, replacement)
    job = sanitize_persisted_value(job)
    with locked_jobs() as (data, _):
        data["jobs"].append(job)
    return (0 if reconciled_observation is not None else result.returncode), {
        "executed": True, "job": job,
    }


def find_job(job_id: str) -> dict[str, Any] | None:
    with locked_jobs() as (data, _):
        for job in reversed(data["jobs"]):
            if job.get("id") == job_id or job.get("name") == job_id:
                return job
    return None


def public_local_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if key != "lease_token"}


def run_local_job_action(job: dict[str, Any], action: str) -> int:
    if action == "status":
        current = find_job(job["id"]) or job
        print_json({"job": public_local_job(current)})
        return 0

    if action == "logs":
        log_path = Path(job.get("log_path") or local_job_log_path(job["id"]))
        output = log_path.read_text(errors="replace")[-12000:] if log_path.exists() else ""
        print_json({"job_id": job["id"], "action": action, "stdout": output})
        return 0

    if action == "cancel":
        if job.get("state") in TERMINAL_LOCAL_STATES:
            print_json({"job_id": job["id"], "action": action, "cancelled": False, "state": job["state"]})
            return 0
        update_job(job["id"], cancel_requested=True, cancel_requested_at=int(time.time()))
        process_pid = int(job.get("process_pid", 0))
        if process_pid:
            try:
                os.killpg(process_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        print_json({"job_id": job["id"], "action": action, "cancelled": True})
        return 0

    if action == "cleanup":
        if job.get("state") not in TERMINAL_LOCAL_STATES:
            raise SystemExit("local job must be terminal before cleanup")
        log_path = Path(job.get("log_path") or local_job_log_path(job["id"]))
        log_path.unlink(missing_ok=True)
        update_job(job["id"], cleaned_at=int(time.time()), log_path=None)
        print_json({"job_id": job["id"], "action": action, "log_deleted": True})
        return 0

    raise SystemExit(f"unsupported local job action: {action}")


def refresh_remote_job(job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    provider = get_provider(job["provider"])
    result = subprocess.run(provider.status_command(job), text=True, capture_output=True)
    observation = provider.parse_status(result.stdout, result.stderr, result.returncode, job)
    changes: dict[str, Any] = {
        "last_checked_at": int(time.time()),
        "provider_absent": observation.absent,
        "provider_evidence": sanitize_persisted_value(observation.evidence),
    }
    if observation.provider_id:
        changes["provider_id"] = observation.provider_id
    if observation.state and should_accept_remote_transition(job.get("state"), observation.state):
        changes["state"] = observation.state
        if observation.state == "running" and not job.get("started_at"):
            changes["started_at"] = int(time.time())
        if observation.state in REMOTE_TERMINAL_STATES:
            changes["completed_at"] = job.get("completed_at") or int(time.time())
    current = update_job(job["id"], **changes) or {**job, **changes}
    if observation.state in REMOTE_TERMINAL_STATES and current.get("result_artifact"):
        collected, _ = collect_result_artifact(current)
        if collected:
            current = find_job(job["id"]) or current
    provider_result = sanitize_persisted_value({
        "returncode": result.returncode,
        "observation": {
            "state": observation.state,
            "provider_id": observation.provider_id,
            "absent": observation.absent,
            "evidence": observation.evidence,
        },
    })
    if observation.absent:
        provider_result["returncode"] = 0
    return current, provider_result


def verify_remote_absence(job: dict[str, Any], attempts: int = 3) -> tuple[bool, dict[str, Any]]:
    """Verify provider compute is gone, allowing short eventual-consistency lag."""
    latest: dict[str, Any] = {}
    for attempt in range(attempts):
        current, latest = refresh_remote_job(job)
        if current.get("provider_absent"):
            return True, latest
        if attempt < attempts - 1:
            time.sleep(1)
    return False, latest


def run_job_action(job_id: str, action: str, discard_results: bool = False) -> int:
    job = find_job(job_id)
    if job is None:
        raise SystemExit(f"unknown job: {job_id}")
    if job.get("provider") == "local":
        return run_local_job_action(job, action)
    provider = get_provider(job["provider"])

    if action == "status":
        current, provider_result = refresh_remote_job(job)
        print_json({
            "job_id": job["id"], "action": action, "job": public_job_view(current),
            "provider_result": public_provider_result(provider_result),
        })
        return provider_result["returncode"]

    if action == "logs":
        current = job
        if current.get("result", {}).get("state") == "collected":
            print_json({
                "job_id": job["id"], "action": action, "source": "verified result bundle",
                "stdout": current["result"].get("stdout", ""),
                "stderr": current["result"].get("stderr", ""),
            })
            return 0
        if not current.get("provider_id"):
            current, _ = refresh_remote_job(current)
        try:
            command = provider.logs_command(current)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        result = subprocess.run(command, text=True, capture_output=True)
        print_json({
            "job_id": job["id"], "name": job["name"], "provider": job["provider"],
            "provider_id": current.get("provider_id"), "action": action,
            "returncode": result.returncode,
            "stdout": redact_sensitive_text(result.stdout[-12000:]),
            "stderr": redact_sensitive_text(result.stderr[-12000:]),
        })
        return result.returncode

    if action == "results":
        current, provider_result = refresh_remote_job(job)
        collected, result_value = collect_result_artifact(current)
        print_json({
            "job_id": job["id"], "action": action, "collected": collected,
            "result": result_value, "provider_result": public_provider_result(provider_result),
        })
        return 0 if collected else 1

    current, status_result = refresh_remote_job(job)
    if action == "cancel":
        if current.get("provider_absent"):
            prior_state = current.get("state")
            next_state = prior_state if prior_state in REMOTE_TERMINAL_STATES else "completed"
            update_job(job["id"], state=next_state, completed_at=current.get("completed_at") or int(time.time()))
            print_json({
                "job_id": job["id"], "action": action, "cancelled": False,
                "already_absent": True, "state": next_state,
            })
            return 0
        update_job(job["id"], state="cancelling", cancel_requested_at=int(time.time()))
        try:
            command = provider.cancel_command(current)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        result = subprocess.run(command, text=True, capture_output=True)
        cancelled = False
        if result.returncode == 0:
            cancelled, verification = verify_remote_absence(current)
            update_job(
                job["id"], state="cancelled" if cancelled else "cancelling",
                completed_at=int(time.time()) if cancelled else None,
                provider_absent=cancelled, cancel_verification=verification,
            )
        print_json({
            "job_id": job["id"], "action": action, "returncode": result.returncode,
            "provider_output": "suppressed",
            "error": redact_sensitive_text(result.stderr[-2000:]),
            "cancelled": cancelled,
            "next": f"elsewhere job-cleanup {job['id']}",
        })
        return 0 if cancelled else (result.returncode or 1)

    if action == "cleanup":
        if current.get("state") not in REMOTE_TERMINAL_STATES:
            print_json({
                "job_id": job["id"], "action": action, "state": current.get("state"),
                "reason": "cleanup is available only after the job reaches a terminal state",
            })
            return 1
        if (
            current.get("result_artifact")
            and (
                current.get("submission", {}).get("returncode", 0) == 0
                or current.get("submission_reconciled") is True
            )
            and current.get("state") not in {"cancelled", "submission_failed"}
        ):
            collected, result_evidence = collect_result_artifact(current)
            if not collected:
                if not discard_results:
                    update_job(job["id"], cleanup_blocked={"result_collection": result_evidence})
                    print_json({
                        "job_id": job["id"], "action": action, "state": current.get("state"),
                        "reason": "verified results are unavailable; rerun with --discard-results to delete resources anyway",
                        "result_collection": result_evidence,
                    })
                    return 1
                update_job(job["id"], result={
                    "state": "discarded", "discarded_at": int(time.time()),
                    "reason": "caller explicitly discarded unavailable results during cleanup",
                })
            current = find_job(job["id"]) or current
        update_job(job["id"], state="cleaning", cleanup_started_at=int(time.time()))
        compute_result = {"deleted": False, "already_absent": bool(current.get("provider_absent"))}
        compute_ok = bool(current.get("provider_absent"))
        if not compute_ok:
            try:
                command = provider.cleanup_command(current)
            except ValueError as error:
                raise SystemExit(str(error)) from error
            result = subprocess.run(command, text=True, capture_output=True)
            compute_result = {
                "deleted": result.returncode == 0, "already_absent": False,
                "returncode": result.returncode, "provider_output": "suppressed",
                "error": redact_sensitive_text(result.stderr[-2000:]),
            }
            if result.returncode == 0:
                compute_ok, verification = verify_remote_absence(current)
                compute_result["verified_absent"] = compute_ok
                compute_result["verification"] = public_provider_result(verification)
            else:
                compute_ok = False
        config = load_config()
        source_artifact = cleanup_source_artifact(current.get("source_artifact", {}), config) if current.get("source_artifact") else {"deleted": False, "reason": "no source artifact"}
        result_artifact = cleanup_source_artifact(current.get("result_artifact", {}), config) if current.get("result_artifact") else {"deleted": False, "reason": "no result artifact"}
        source_ok = bool(
            source_artifact.get("verified_absent") is True
            or source_artifact.get("reason") == "no source artifact"
        )
        result_ok = bool(
            result_artifact.get("verified_absent") is True
            or result_artifact.get("reason") == "no result artifact"
        )
        final_state = "cleaned" if compute_ok and source_ok and result_ok else "cleanup_failed"
        update_job(
            job["id"], state=final_state, cleaned_at=int(time.time()) if final_state == "cleaned" else None,
            cleanup={
                "compute": compute_result,
                "source_artifact": source_artifact,
                "result_artifact": result_artifact,
            },
            provider_absent=compute_ok,
        )
        print_json({
            "job_id": job["id"], "action": action, "state": final_state,
            "compute": compute_result, "source_artifact": source_artifact,
            "result_artifact": result_artifact,
            "status_before_cleanup": public_provider_result(status_result),
        })
        return 0 if final_state == "cleaned" else 1

    raise SystemExit(f"unsupported job action: {action}")


def job_summary(job: dict[str, Any]) -> dict[str, Any]:
    state = job.get("state", "unknown")
    reason = None
    if state == "waiting_for_capacity":
        reason = job.get("last_admission", {}).get("reason", "waiting for local capacity")
    elif state == "running":
        reason = "running on this machine"
    elif state == "submitted":
        reason = f"submitted to {job.get('provider')}; use job status to refresh provider state"
    elif state in {"failed", "submission_failed"}:
        reason = job.get("error") or job.get("submission", {}).get("stderr", "job failed")[-500:]
    command = job.get("command", "")
    if isinstance(command, list):
        command = shlex.join(command)
    returncode = job.get("returncode")
    if returncode is None:
        returncode = job.get("submission", {}).get("returncode")
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "owner": job.get("owner") or "remote-dispatch",
        "provider": job.get("provider"),
        "workload": job.get("workload"),
        "state": state,
        "reason": reason,
        "command": command,
        "source_path": job.get("source_path"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at") or job.get("submitted_at"),
        "completed_at": job.get("completed_at"),
        "returncode": returncode,
        "can_cancel": state not in TERMINAL_JOB_STATES,
        "status_command": f"elsewhere job-status {job.get('id')}",
        "logs_command": f"elsewhere job-logs {job.get('id')}",
        "results_command": f"elsewhere job-results {job.get('id')}",
        "cancel_command": f"elsewhere job-cancel {job.get('id')}",
    }


def queue_snapshot(history_limit: int = 20) -> dict[str, Any]:
    with locked_jobs() as (job_data, _):
        jobs = [dict(job) for job in job_data.get("jobs", [])]
    with locked_state() as (lease_data, _):
        leases = [dict(lease) for lease in lease_data.get("leases", [])]
        capacity = payload(system_metrics(), leases)

    active = [job_summary(job) for job in jobs if job.get("state") not in TERMINAL_JOB_STATES]
    history = [job_summary(job) for job in jobs if job.get("state") in TERMINAL_JOB_STATES]
    active.sort(key=lambda job: int(job.get("created_at") or 0), reverse=True)
    history.sort(key=lambda job: int(job.get("created_at") or 0), reverse=True)

    lease_rows = []
    for lease in leases:
        match = next(
            (
                job for job in jobs
                if job.get("lease_token") == lease.get("token")
                or (job.get("state") == "running" and job.get("owner") == lease.get("owner"))
            ),
            None,
        )
        lease_rows.append({
            "token": lease.get("token"),
            "owner": lease.get("owner"),
            "workload": lease.get("workload"),
            "count": lease.get("count"),
            "reserved_mb": lease.get("reserved_mb"),
            "created_at": lease.get("created_at"),
            "expires_at": lease.get("expires_at"),
            "tracked_job_id": match.get("id") if match else None,
            "visibility": "tracked job" if match else "standalone reservation",
            "release_command": f"elsewhere release {lease.get('token')}",
        })

    return {
        "checked_at": int(time.time()),
        "capacity": capacity,
        "counts": {
            "waiting": sum(job["state"] == "waiting_for_capacity" for job in active),
            "local_running": sum(job["state"] == "running" for job in active),
            "remote_submitted": sum(job["state"] == "submitted" for job in active),
            "reservations": len(lease_rows),
        },
        "active_jobs": active,
        "history": history[:history_limit],
        "leases": lease_rows,
    }


def human_queue(snapshot: dict[str, Any]) -> None:
    memory = snapshot["capacity"]["memory"]
    band = snapshot["capacity"]["capacity_band"]
    counts = snapshot["counts"]
    print(
        f"Elsewhere · {band['name']} · {memory['memory_level']}% memory headroom · "
        f"{counts['waiting']} waiting · {counts['local_running']} local running · "
        f"{counts['remote_submitted']} remote submitted · "
        f"{counts['reservations']} reservations"
    )
    if snapshot["active_jobs"]:
        print("\nWORK")
        for job in snapshot["active_jobs"]:
            print(f"  {job['state']:<21} {job['owner']:<28} {job['workload']:<14} {job['id'][:10]}")
            if job.get("reason"):
                print(f"    {job['reason']}")
            print(f"    {job['command']}")
            if job["can_cancel"]:
                print(f"    cancel: {job['cancel_command']}")
    else:
        print("\nNo waiting or running work.")
    if snapshot["leases"]:
        print("\nRESERVATIONS")
        for lease in snapshot["leases"]:
            print(
                f"  {lease['owner']:<28} {lease['workload']:<14} "
                f"{lease['reserved_mb']} MB · {lease['visibility']}"
            )
            print(f"    release: {lease['release_command']}")
    if snapshot["history"]:
        print("\nRECENT")
        for job in snapshot["history"][:8]:
            print(f"  {job['state']:<21} {job['owner']:<28} {job['id'][:10]}")


def capture_job_action(
    job_id: str, action: str, discard_results: bool = False
) -> tuple[int, dict[str, Any]]:
    with ACTION_CAPTURE_LOCK:
        original_stdout = sys.stdout
        buffer = io.StringIO()
        try:
            sys.stdout = buffer
            code = run_job_action(job_id, action, discard_results=discard_results)
        finally:
            sys.stdout = original_stdout
    output = buffer.getvalue().strip()
    try:
        return code, json.loads(output) if output else {"ok": code == 0}
    except json.JSONDecodeError:
        return code, {"ok": code == 0, "output": output}


def dashboard_html(token: str) -> str:
    safe_token = html.escape(token, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Elsewhere — Work in motion</title>
<style>
:root{{--ink:#161713;--paper:#f1efe7;--line:#c7c5ba;--acid:#d8ff45;--quiet:#696b63;--danger:#b5442f}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font-family:"Avenir Next",Avenir,"Helvetica Neue",sans-serif}}
header{{display:grid;grid-template-columns:1fr auto;align-items:end;padding:28px 34px;border-bottom:1px solid var(--ink)}}
.mark{{font-family:"Iowan Old Style",Baskerville,serif;font-size:clamp(44px,7vw,92px);line-height:.78;letter-spacing:-.065em}}
.mark i{{font-weight:400}} .stamp{{font:600 11px/1.2 "SFMono-Regular",Consolas,monospace;letter-spacing:.12em;text-transform:uppercase}}
main{{padding:26px 34px 60px}} .meter{{display:grid;grid-template-columns:repeat(6,1fr);border:1px solid var(--ink);margin-bottom:42px}}
.metric{{padding:14px 16px;border-right:1px solid var(--ink)}} .metric:last-child{{border:0}} .metric b{{display:block;font:500 30px/1 "Iowan Old Style",serif}}
.metric span{{font:600 10px/1.4 "SFMono-Regular",monospace;text-transform:uppercase;letter-spacing:.1em;color:var(--quiet)}}
.section-head{{display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid var(--ink);padding-bottom:8px;margin-top:34px}}
h2{{font:500 17px/1 "SFMono-Regular",monospace;text-transform:uppercase;letter-spacing:.12em;margin:0}} .section-head span{{color:var(--quiet);font-size:12px}}
.row{{display:grid;grid-template-columns:160px minmax(150px,1fr) 130px 2fr auto;gap:18px;align-items:start;padding:18px 0;border-bottom:1px solid var(--line)}}
.state{{font:700 10px/1.2 "SFMono-Regular",monospace;text-transform:uppercase;letter-spacing:.08em}} .state.waiting_for_capacity{{background:var(--acid);padding:5px 7px;width:max-content}}
.owner{{font-weight:650}} .sub,.reason{{color:var(--quiet);font-size:12px;margin-top:4px}} code{{font:11px/1.45 "SFMono-Regular",monospace;word-break:break-word}}
button{{border:1px solid var(--ink);background:transparent;color:var(--ink);padding:8px 11px;font:700 10px "SFMono-Regular",monospace;text-transform:uppercase;cursor:pointer}}
button:hover{{background:var(--ink);color:var(--paper)}} button.danger:hover{{background:var(--danger)}} .empty{{padding:34px 0;color:var(--quiet);font-family:"Iowan Old Style",serif;font-size:24px}}
.notice{{display:none;position:fixed;right:20px;bottom:20px;background:var(--ink);color:var(--paper);padding:12px 16px;font-size:12px}}
@media(max-width:800px){{header,main{{padding-left:18px;padding-right:18px}}.meter{{grid-template-columns:1fr 1fr}}.metric:nth-child(2n){{border-right:0}}.metric:nth-child(-n+4){{border-bottom:1px solid var(--ink)}}.row{{grid-template-columns:1fr 1fr}}.row code,.row .reason{{grid-column:1/-1}}}}
</style>
</head>
<body>
<header><div class="mark">Else<i>where</i></div><div class="stamp">Work in motion<br><span id="checked">connecting</span></div></header>
<main><div class="meter" id="meter"></div><section><div class="section-head"><h2>Now</h2><span>Refreshes every 3 seconds</span></div><div id="jobs"></div></section><section><div class="section-head"><h2>Reservations</h2><span>Capacity claimed outside or by running work</span></div><div id="leases"></div></section><section><div class="section-head"><h2>Recently finished</h2><span>Latest 20</span></div><div id="history"></div></section></main>
<div class="notice" id="notice"></div>
<script>
const token="{safe_token}"; const esc=v=>String(v??"");
function el(tag,cls,text){{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n}}
function metric(value,label){{const n=el('div','metric');n.append(el('b','',value),el('span','',label));return n}}
function jobRow(job,history=false){{const r=el('div','row');r.append(el('div','state '+job.state,job.state.replaceAll('_',' ')));const o=el('div');o.append(el('div','owner',job.owner),el('div','sub',job.workload+' · '+job.provider+' · '+job.id.slice(0,10)));r.append(o,el('div','',history?(job.returncode===null?'done':'exit '+job.returncode):''),el('code','',job.command||''));const a=el('div');if(job.can_cancel){{const b=el('button','danger','Cancel');b.onclick=()=>act('/api/jobs/'+job.id+'/cancel','Cancel this work?');a.append(b)}}r.append(a);if(job.reason){{const why=el('div','reason',job.reason);why.style.gridColumn='2/-1';r.append(why)}}return r}}
function leaseRow(x){{const r=el('div','row');r.append(el('div','state',x.visibility),el('div','owner',x.owner),el('div','',x.reserved_mb+' MB'),el('code','',x.workload+' × '+x.count));const a=el('div');const b=el('button','danger','Release');b.onclick=()=>act('/api/leases/'+x.token+'/release','Release '+x.owner+'? Running work may lose its reservation.');a.append(b);r.append(a);return r}}
async function act(path,question){{if(!confirm(question))return;const res=await fetch(path,{{method:'POST',headers:{{'X-Elsewhere-Token':token,'Content-Type':'application/json'}},body:'{{}}'}});const data=await res.json();notice(data);await load()}}
function notice(data){{const n=document.getElementById('notice');n.textContent=data.error||data.action||data.released?'Updated':'Done';n.style.display='block';setTimeout(()=>n.style.display='none',1800)}}
function fill(id,items,render,empty){{const root=document.getElementById(id);root.replaceChildren();if(!items.length)root.append(el('div','empty',empty));else items.forEach(x=>root.append(render(x)))}}
async function load(){{const res=await fetch('/api/snapshot?token='+token);if(!res.ok)return;const x=await res.json();document.getElementById('checked').textContent=new Date(x.checked_at*1000).toLocaleTimeString();const c=x.capacity,memory=c.memory,swap=memory.swap_known?Math.round(memory.swap_utilization_percent)+'%':'—',activity=c.capacity_band.swap_activity_mb_per_second+' MB/s',m=document.getElementById('meter');m.replaceChildren(metric(c.capacity_band.name,'Capacity'),metric(memory.memory_level+'%','Memory headroom'),metric(swap,'Swap retained · '+activity),metric(x.counts.waiting,'Waiting'),metric(x.counts.local_running,'Local running'),metric(x.counts.reservations,'Reservations'));fill('jobs',x.active_jobs,jobRow,'Nothing waiting. Nothing running.');fill('leases',x.leases,leaseRow,'No active reservations.');fill('history',x.history,j=>jobRow(j,true),'No finished work yet.')}}
load();setInterval(load,3000);
</script></body></html>"""


def run_dashboard(host: str, port: int) -> int:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("dashboard may only bind to the local machine")
    token = uuid.uuid4().hex

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, value: dict[str, Any], status: int = 200) -> None:
            payload_bytes = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload_bytes)

        def authorized(self) -> bool:
            parsed = urlparse(self.path)
            query_token = parse_qs(parsed.query).get("token", [None])[0]
            return query_token == token or self.headers.get("X-Elsewhere-Token") == token

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not self.authorized():
                self.send_json({"error": "dashboard token required"}, 403)
                return
            if parsed.path == "/":
                content = dashboard_html(token).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
                return
            if parsed.path == "/api/snapshot":
                self.send_json(queue_snapshot())
                return
            self.send_json({"error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            if not self.authorized():
                self.send_json({"error": "dashboard token required"}, 403)
                return
            parsed = urlparse(self.path)
            job_match = re.fullmatch(r"/api/jobs/([a-f0-9]+)/cancel", parsed.path)
            lease_match = re.fullmatch(r"/api/leases/([a-f0-9]+)/release", parsed.path)
            try:
                if job_match:
                    code, value = capture_job_action(job_match.group(1), "cancel")
                    self.send_json(value, 200 if code == 0 else 409)
                    return
                if lease_match:
                    code, value = release(lease_match.group(1))
                    self.send_json(value, 200 if code == 0 else 404)
                    return
            except SystemExit as error:
                self.send_json({"error": str(error)}, 400)
                return
            self.send_json({"error": "not found"}, 404)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{server.server_port}/?token={token}"
    print_json({"dashboard": url, "bind": host, "port": server.server_port})
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def mcp_tools() -> list[dict[str, Any]]:
    workload_schema = {"type": "string", "enum": list(WORKLOADS)}
    provider_schema = {"type": "string", "enum": ["auto", *SUPPORTED_PROVIDERS]}
    dispatch_properties = {
        "workload": workload_schema,
        "provider": provider_schema,
        "image": {"type": "string", "minLength": 1},
        "command": {"type": "string", "minLength": 1},
        "cpu": {"type": "integer", "minimum": 1},
        "memory_mb": {"type": "integer", "minimum": 256},
        "source_path": {"type": "string", "description": "Absolute local directory to transport."},
        "git_url": {"type": "string"},
        "git_ref": {"type": "string"},
        "max_runtime_seconds": {"type": "integer", "minimum": 60},
        "estimated_cost_usd": {"type": "number", "exclusiveMinimum": 0},
        "result_paths": {
            "type": "array", "items": {"type": "string", "minLength": 1},
            "maxItems": 32, "default": [],
        },
    }
    return [
        {
            "name": "elsewhere_trust_status",
            "title": "Inspect Elsewhere Trust",
            "description": "Read the active provider, source-boundary, region, cost, runtime, and resource approval receipt.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        },
        {
            "name": "elsewhere_queue",
            "title": "See Elsewhere Work",
            "description": "List waiting, running, and recently finished work plus every active capacity reservation.",
            "inputSchema": {
                "type": "object",
                "properties": {"history_limit": {"type": "integer", "minimum": 0, "maximum": 100}},
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        },
        {
            "name": "elsewhere_plan",
            "title": "Plan Work Elsewhere",
            "description": "Produce a non-billable local-or-remote placement plan and evaluate it against the active trust receipt. Never uploads source or launches compute.",
            "inputSchema": {
                "type": "object",
                "properties": dispatch_properties,
                "required": ["workload", "image", "command", "cpu", "memory_mb", "max_runtime_seconds", "estimated_cost_usd"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        },
        {
            "name": "elsewhere_dispatch",
            "title": "Dispatch Approved Work",
            "description": "Upload the approved source boundary when present and launch remote compute only when every field matches the supplied durable trust receipt. This can incur cloud cost.",
            "inputSchema": {
                "type": "object",
                "properties": {**dispatch_properties, "approval_receipt": {"type": "string", "pattern": "^ew1_[a-f0-9]{32}$"}},
                "required": ["workload", "image", "command", "cpu", "memory_mb", "max_runtime_seconds", "estimated_cost_usd", "approval_receipt"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
        },
        {
            "name": "elsewhere_job_status",
            "title": "Inspect Elsewhere Job",
            "description": "Read status or retained logs for one local or remote job without changing it.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["status", "logs", "results"]},
                    "job_id": {"type": "string"},
                },
                "required": ["action", "job_id"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
        },
        {
            "name": "elsewhere_job_control",
            "title": "Control Elsewhere Work",
            "description": "Cancel or clean up a job, or release a visible local capacity reservation. Use only after the user has decided the work is disposable.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["cancel", "cleanup", "release_reservation"]},
                    "job_id": {"type": "string"},
                    "lease_token": {"type": "string"},
                    "discard_results": {"type": "boolean", "default": False},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
        },
    ]


def dispatch_arguments(arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    runtime = int(arguments["max_runtime_seconds"])
    estimated_cost = float(arguments["estimated_cost_usd"])
    if runtime < 60 or not math.isfinite(estimated_cost) or estimated_cost <= 0:
        raise SystemExit("remote plans require a finite positive cost estimate and at least 60 seconds runtime")
    return build_dispatch_plan(
        arguments["workload"],
        arguments.get("provider", "auto"),
        arguments["image"],
        arguments["command"],
        int(arguments["cpu"]),
        int(arguments["memory_mb"]),
        arguments.get("git_url"),
        arguments.get("git_ref"),
        arguments.get("source_path"),
        runtime,
        estimated_cost,
        arguments.get("result_paths", []),
    )


def mcp_call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "elsewhere_trust_status":
        return trust_status()
    if name == "elsewhere_queue":
        return queue_snapshot(int(arguments.get("history_limit", 20)))
    if name == "elsewhere_plan":
        decision = local_route_decision(arguments["workload"])
        job, plan = dispatch_arguments(arguments)
        return {"decision": decision, "job": job, "plan": plan, "trust": plan["trust"]}
    if name == "elsewhere_dispatch":
        job, plan = dispatch_arguments(arguments)
        config = load_config()
        receipt = arguments["approval_receipt"]
        trust = require_trust(job, plan, config, receipt)
        job, plan = attach_execution_artifacts(
            job, arguments["command"], int(arguments["max_runtime_seconds"]), config, receipt
        )
        trust = plan["trust"]
        code, value = execute_dispatch(job, plan, receipt)
        value["trust"] = trust
        value["returncode"] = code
        return value
    if name == "elsewhere_job_status":
        _, value = capture_job_action(arguments["job_id"], arguments["action"])
        return value
    if name == "elsewhere_job_control":
        action = arguments["action"]
        if action == "release_reservation":
            token = arguments.get("lease_token")
            if not token:
                raise SystemExit("lease_token is required for release_reservation")
            _, value = release(token)
            return value
        job_id = arguments.get("job_id")
        if not job_id:
            raise SystemExit("job_id is required for this action")
        _, value = capture_job_action(
            job_id, action, discard_results=bool(arguments.get("discard_results", False))
        )
        return value
    raise SystemExit(f"unknown Elsewhere tool: {name}")


def mcp_result(request_id: Any, result: dict[str, Any]) -> None:
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, separators=(",", ":")), flush=True)


def mcp_error(request_id: Any, code: int, message: str, data: Any | None = None) -> None:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = sanitize_persisted_value(data)
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": error}, separators=(",", ":")), flush=True)


def validate_schema(value: Any, schema: dict[str, Any], path: str = "arguments") -> None:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        properties = schema.get("properties", {})
        missing = [name for name in schema.get("required", []) if name not in value]
        if missing:
            raise ValueError(f"{path} is missing required fields: {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise ValueError(f"{path} has unknown fields: {', '.join(extra)}")
        for name, item in value.items():
            if name in properties:
                validate_schema(item, properties[name], f"{path}.{name}")
        return
    if expected == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        if len(value) > int(schema.get("maxItems", len(value))):
            raise ValueError(f"{path} has too many items")
        for index, item in enumerate(value):
            validate_schema(item, schema.get("items", {}), f"{path}[{index}]")
        return
    if expected == "string":
        if not isinstance(value, str):
            raise ValueError(f"{path} must be a string")
        if len(value) < int(schema.get("minLength", 0)):
            raise ValueError(f"{path} is too short")
        if len(value) > int(schema.get("maxLength", len(value))):
            raise ValueError(f"{path} is too long")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"{path} must be one of: {', '.join(schema['enum'])}")
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], value):
            raise ValueError(f"{path} has an invalid format")
        return
    if expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path} must be an integer")
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"{path} must be a finite number")
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{path} must be a boolean")
    if expected in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} must be at most {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ValueError(f"{path} must be greater than {schema['exclusiveMinimum']}")


def run_mcp_server() -> int:
    initialized = False
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
                request_id = message.get("id") if isinstance(message, dict) else None
                mcp_error(request_id, -32600, "invalid request")
                continue
            method = message.get("method")
            request_id = message.get("id")
            if request_id is None and method != "notifications/initialized":
                continue
            if method == "initialize":
                params = message.get("params", {})
                if not isinstance(params, dict) or not isinstance(params.get("protocolVersion"), str):
                    mcp_error(request_id, -32602, "invalid initialize parameters")
                    continue
                initialized = True
                mcp_result(request_id, {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "elsewhere", "version": __version__},
                    "instructions": "Inspect trust and plan first. Dispatch only with the active receipt. Use the queue before releasing reservations.",
                })
            elif method == "notifications/initialized":
                # This notification confirms an initialize exchange; it cannot replace one.
                if not initialized:
                    continue
            elif method == "ping":
                mcp_result(request_id, {})
            elif method == "tools/list":
                if not initialized:
                    mcp_error(request_id, -32002, "server not initialized")
                    continue
                mcp_result(request_id, {"tools": mcp_tools()})
            elif method == "tools/call":
                if not initialized:
                    mcp_error(request_id, -32002, "server not initialized")
                    continue
                params = message.get("params", {})
                if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                    mcp_error(request_id, -32602, "invalid tool call parameters")
                    continue
                arguments = params.get("arguments", {})
                tool = next((item for item in mcp_tools() if item["name"] == params["name"]), None)
                if tool is None:
                    mcp_error(request_id, -32602, "unknown tool", {"name": params["name"]})
                    continue
                try:
                    validate_schema(arguments, tool["inputSchema"])
                except ValueError as error:
                    mcp_error(request_id, -32602, str(error))
                    continue
                try:
                    value = sanitize_persisted_value(mcp_call_tool(params["name"], arguments))
                    mcp_result(request_id, {
                        "content": [{"type": "text", "text": json.dumps(value, indent=2, sort_keys=True)}],
                        "structuredContent": value,
                    })
                except (SystemExit, KeyError, ValueError, OSError, subprocess.SubprocessError) as error:
                    mcp_result(request_id, {
                        "content": [{"type": "text", "text": redact_sensitive_text(str(error))}],
                        "isError": True,
                    })
            elif request_id is not None:
                mcp_error(request_id, -32601, "method not found")
        except json.JSONDecodeError as error:
            mcp_error(None, -32700, "parse error", str(error))
    return 0


def prompt_init_value(value: str, label: str, hint: str) -> str:
    if value:
        return value
    if not sys.stdin.isatty():
        raise SystemExit(f"{label} is required in non-interactive mode; {hint}")
    entered = input(f"{label}: ").strip()
    if not entered:
        raise SystemExit(f"{label} is required; {hint}")
    return entered


def human_init(value: dict[str, Any]) -> None:
    print(f"Elsewhere configuration created at {value['created']}")
    print(f"Compute: {value['provider']}")
    print(f"Artifacts: {value['artifact_store']}")
    print("\nNext:")
    for index, action in enumerate(value["next"], start=1):
        print(f"  {index}. {action}")


def human_doctor(value: dict[str, Any]) -> None:
    for item in value["checks"]:
        symbol = {"pass": "✓", "warn": "!", "fail": "✗"}[item["status"]]
        print(f"{symbol} {item['name']}: {item['message']}")
        if item.get("next"):
            print(f"    Next: {item['next']}")
    if value["ready_for_execution"]:
        print("\nReady for dry plans and approved remote execution.")
    elif value["ready_for_planning"]:
        print("\nReady for dry plans. Complete the trust step before remote execution.")
    else:
        print("\nSetup needs attention before Elsewhere can plan a remote run.")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Decide whether work should run here or on approved cloud compute, "
            "then keep its lifecycle visible."
        ),
        epilog=(
            "New here? Run `elsewhere status --human`, then try a local dry plan:\n"
            "  elsewhere route --workload light --execution local "
            "--command 'printf elsewhere-ok'\n"
            "Remote routes stay non-billable unless you add --execute."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"elsewhere {__version__}")
    commands = parser.add_subparsers(
        dest="command", required=True, metavar="COMMAND"
    )

    status_parser = commands.add_parser(
        "status", help="show local capacity, reservations, and safe concurrency"
    )
    status_parser.add_argument("--human", action="store_true")

    queue_parser = commands.add_parser(
        "queue", help="list waiting and running local work"
    )
    queue_parser.add_argument("--json", action="store_true")
    queue_parser.add_argument("--history-limit", type=int, default=20)

    cleanup_parser = commands.add_parser(
        "cleanup", help="remove expired leases and orphaned local jobs"
    )
    cleanup_parser.add_argument("--stale", action="store_true", required=True)

    dashboard_parser = commands.add_parser(
        "dashboard", help="open the loopback queue and capacity control room"
    )
    dashboard_parser.add_argument("--host", default="127.0.0.1")
    dashboard_parser.add_argument("--port", type=int, default=8799)

    sample_parser = commands.add_parser("sample-memory")
    sample_parser.add_argument("--quiet", action="store_true")
    commands.add_parser(
        "sampler-install", help="install the private macOS capacity sampler"
    )
    commands.add_parser(
        "sampler-status", help="check the macOS capacity sampler"
    )
    commands.add_parser(
        "sampler-remove", help="remove the macOS capacity sampler"
    )

    commands.add_parser(
        "trust-status", help="inspect the active remote-execution boundary"
    )
    trust_approve_parser = commands.add_parser(
        "trust-approve", help="approve exact providers, source roots, and limits"
    )
    trust_approve_parser.add_argument("--path", default=str(global_config_path()))
    trust_approve_parser.add_argument("--provider", action="append", choices=SUPPORTED_PROVIDERS, required=True)
    trust_approve_parser.add_argument("--source-root", action="append", required=True)
    trust_approve_parser.add_argument("--allow-uncommitted", action=argparse.BooleanOptionalAction, default=False)
    trust_approve_parser.add_argument("--allow-private", action=argparse.BooleanOptionalAction, default=False)
    trust_approve_parser.add_argument("--max-cpu", type=int, default=4)
    trust_approve_parser.add_argument("--max-memory-mb", type=int, default=8192)
    trust_approve_parser.add_argument("--max-runtime-seconds", type=int, default=3600)
    trust_approve_parser.add_argument("--max-estimated-cost-usd", type=float, default=5.0)
    trust_approve_parser.add_argument("--expires-days", type=int, default=180)
    trust_revoke_parser = commands.add_parser(
        "trust-revoke", help="revoke an approved execution boundary"
    )
    trust_revoke_parser.add_argument("--path", default=str(global_config_path()))

    commands.add_parser(
        "mcp-server", help="serve typed Elsewhere tools over MCP"
    )

    recommend_parser = commands.add_parser(
        "recommend", help="show safe local concurrency for a workload"
    )
    recommend_parser.add_argument("--workload", choices=WORKLOADS, required=True)
    recommend_parser.add_argument("--max-count", type=int, default=8)

    acquire_parser = commands.add_parser(
        "acquire", help="reserve shared local capacity"
    )
    acquire_parser.add_argument("--workload", choices=WORKLOADS, required=True)
    acquire_parser.add_argument("--count", type=int, default=1)
    acquire_parser.add_argument("--owner", default="agent")
    acquire_parser.add_argument("--ttl", type=int, default=DEFAULT_TTL)

    release_parser = commands.add_parser(
        "release", help="release a shared-capacity reservation"
    )
    release_parser.add_argument("token")

    renew_parser = commands.add_parser(
        "renew", help="extend a shared-capacity reservation"
    )
    renew_parser.add_argument("token")
    renew_parser.add_argument("--ttl", type=int, default=DEFAULT_TTL)

    run_parser = commands.add_parser(
        "run", help="run or queue one command under a managed local lease"
    )
    run_parser.add_argument("--workload", choices=WORKLOADS, required=True)
    run_parser.add_argument("--count", type=int, default=1)
    run_parser.add_argument("--owner", default="agent")
    run_parser.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    run_parser.add_argument("--queue", action=argparse.BooleanOptionalAction, default=True)
    run_parser.add_argument("--poll-seconds", type=float, default=5.0)
    run_parser.add_argument("remainder", nargs=argparse.REMAINDER)

    commands.add_parser(
        "providers", help="show provider readiness and configured routing"
    )

    init_parser = commands.add_parser("init", help="create a guided Fly/Tigris or Azure configuration")
    init_parser.add_argument("--path", default=".elsewhere.json")
    init_parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS, default="fly")
    init_parser.add_argument("--fly-app", default=os.environ.get("AGENT_CAPACITY_FLY_APP", ""))
    init_parser.add_argument("--fly-org", default=os.environ.get("AGENT_CAPACITY_FLY_ORG", ""))
    init_parser.add_argument("--fly-region", default="bom")
    init_parser.add_argument("--fly-region-fallback", action="append", default=[])
    init_parser.add_argument("--tigris-bucket", default=os.environ.get("BUCKET_NAME", ""))
    init_parser.add_argument("--azure-subscription", default=os.environ.get("AGENT_CAPACITY_AZURE_SUBSCRIPTION", ""))
    init_parser.add_argument("--azure-resource-group", default=os.environ.get("AGENT_CAPACITY_AZURE_RESOURCE_GROUP", ""))
    init_parser.add_argument("--azure-location", default="centralindia")
    init_parser.add_argument("--azure-storage-account", default=os.environ.get("AGENT_CAPACITY_AZURE_STORAGE_ACCOUNT", ""))
    init_parser.add_argument("--source-root", default=".")
    init_parser.add_argument("--force", action="store_true")
    init_parser.add_argument("--json", action="store_true")

    doctor_parser = commands.add_parser("doctor", help="check readiness without uploading or starting compute")
    doctor_parser.add_argument("--source-path")
    doctor_parser.add_argument("--json", action="store_true")

    config_parser = commands.add_parser(
        "init-config", help="write an advanced configuration template"
    )
    config_parser.add_argument("--path", default=".elsewhere.json")
    config_parser.add_argument("--force", action="store_true")

    dispatch_parser = commands.add_parser(
        "dispatch", help="plan or execute work on a chosen provider"
    )
    dispatch_parser.add_argument("--workload", choices=WORKLOADS, required=True)
    dispatch_parser.add_argument("--provider", choices=("auto", *SUPPORTED_PROVIDERS), default="auto")
    dispatch_parser.add_argument("--image", required=True)
    dispatch_parser.add_argument("--command", dest="workload_command", required=True)
    dispatch_parser.add_argument("--cpu", type=int, default=2)
    dispatch_parser.add_argument("--memory-mb", type=int, default=4096)
    dispatch_parser.add_argument("--git-url")
    dispatch_parser.add_argument("--git-ref")
    dispatch_parser.add_argument("--source-path")
    dispatch_parser.add_argument("--max-runtime-seconds", type=int, default=3600)
    dispatch_parser.add_argument("--estimated-cost-usd", type=float, default=0)
    dispatch_parser.add_argument("--approval-receipt")
    dispatch_parser.add_argument("--result-path", action="append", default=[])
    dispatch_parser.add_argument("--execute", action="store_true")

    route_parser = commands.add_parser(
        "route", help="plan or execute a local-versus-remote placement decision"
    )
    route_parser.add_argument("--workload", choices=WORKLOADS, required=True)
    route_parser.add_argument("--execution", choices=("auto", "local", "remote"), default="auto")
    route_parser.add_argument("--provider", choices=("auto", *SUPPORTED_PROVIDERS), default="auto")
    route_parser.add_argument("--image")
    route_parser.add_argument("--command", dest="workload_command", required=True)
    route_parser.add_argument("--cpu", type=int, default=2)
    route_parser.add_argument("--memory-mb", type=int, default=4096)
    route_parser.add_argument("--git-url")
    route_parser.add_argument("--git-ref")
    route_parser.add_argument("--source-path")
    route_parser.add_argument("--max-runtime-seconds", type=int, default=3600)
    route_parser.add_argument("--estimated-cost-usd", type=float, default=0)
    route_parser.add_argument("--approval-receipt")
    route_parser.add_argument("--result-path", action="append", default=[])
    route_parser.add_argument("--owner", default="router")
    route_parser.add_argument("--queue", action=argparse.BooleanOptionalAction, default=True)
    route_parser.add_argument("--poll-seconds", type=float, default=5.0)
    route_parser.add_argument("--execute", action="store_true")

    worker_parser = commands.add_parser("_local-worker")
    worker_parser.add_argument("job_id")

    job_help = {
        "job-status": "refresh and show one job's lifecycle state",
        "job-logs": "show privacy-safe logs for one job",
        "job-results": "recover and verify one job's result bundle",
        "job-cancel": "cancel one local or remote job",
        "job-cleanup": "remove one job after result protection and absence checks",
    }
    for action, help_text in job_help.items():
        action_parser = commands.add_parser(action, help=help_text)
        action_parser.add_argument("job_id")
        if action == "job-cleanup":
            action_parser.add_argument("--discard-results", action="store_true")
    hidden_commands = {"sample-memory", "_local-worker"}
    commands._choices_actions = [
        action for action in commands._choices_actions
        if action.dest not in hidden_commands
    ]
    return parser


def validate_count_and_ttl(count: int, ttl: int) -> None:
    if count < 1:
        raise SystemExit("count must be at least 1")
    if ttl < 60 or ttl > MAX_TTL:
        raise SystemExit(f"ttl must be between 60 and {MAX_TTL} seconds")


def human_status(value: dict[str, Any]) -> None:
    memory = value["memory"]
    budget = value["budget"]
    if not memory.get("sensing_available", True):
        print("Local capacity sensing: unavailable on this platform — remote execution still works")
    print(f"Memory headroom: {memory['memory_level']}% ({budget['headroom_mb']} MB estimated)")
    print(f"Capacity: {value['capacity_band']['name']} — {value['capacity_band']['reason']}")
    if memory.get("swap_known"):
        print(
            f"Swap retained: {memory.get('swap_used_mb', 0)} / {memory.get('swap_total_mb', 0)} MB "
            f"({memory.get('swap_utilization_percent', 0)}%)"
        )
        print(
            f"Swap activity: {value['capacity_band']['swap_activity_mb_per_second']} MB/second "
            f"({value['capacity_band']['swap_activity_per_second']} pages/second)"
        )
    else:
        print("Swap: unavailable (install the host sampler for sandbox-safe telemetry)")
    print(f"Protected reserve: {budget['reserve_mb']} MB")
    print(
        f"Outstanding reservations: {budget['leased_mb']} MB declared; "
        f"{budget['effective_leased_mb']} MB admission impact"
    )
    print(f"Swap safety margin: {budget['swap_penalty_mb']} MB")
    print(f"Available for new work: {budget['available_mb']} MB")
    print("Safe new concurrency:")
    for workload, count in value["recommendations"].items():
        print(f"  {workload}: {count}")
    if value["active_leases"]:
        print("Active leases:")
        for lease in value["active_leases"]:
            print(f"  {lease['owner']}: {lease['workload']} x{lease['count']} until {lease['expires_at']}")


def sampler_plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{SAMPLER_LABEL}.plist"


def sampler_install() -> dict[str, Any]:
    if host_platform() == "linux":
        raise SystemExit(
            "the host memory sampler is not needed on Linux — Elsewhere reads "
            "/proc/pressure/memory and /proc/meminfo directly on every call"
        )
    if sys.platform != "darwin" or not shutil.which("launchctl"):
        raise SystemExit("the host memory sampler currently supports macOS only")
    executable = Path(sys.argv[0]).resolve()
    plist_path = sampler_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": SAMPLER_LABEL,
        "ProgramArguments": [sys.executable, str(executable), "sample-memory", "--quiet"],
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "RunAtLoad": True,
        "StartInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(runtime_dir() / "memory-sampler.error.log"),
    }
    runtime_dir().mkdir(parents=True, exist_ok=True)
    runtime_dir().chmod(0o700)
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=True)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], capture_output=True)
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)], text=True, capture_output=True
    )
    if result.returncode:
        raise SystemExit(result.stderr.strip() or "launchctl could not start the Elsewhere memory sampler")
    deadline = time.time() + 3
    while time.time() < deadline and not read_host_sample():
        time.sleep(0.1)
    return {"installed": True, "label": SAMPLER_LABEL, "plist": str(plist_path), **sampler_status()}


def sampler_status() -> dict[str, Any]:
    sample = read_host_sample()
    if not shutil.which("launchctl"):
        return {
            "loaded": False, "sample_fresh": sample is not None,
            "sample_path": str(host_metrics_path()), "sample": sample,
        }
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{SAMPLER_LABEL}"], text=True, capture_output=True
    )
    return {
        "loaded": result.returncode == 0,
        "sample_fresh": sample is not None,
        "sample_path": str(host_metrics_path()),
        "sample": sample,
    }


def sampler_remove() -> dict[str, Any]:
    if not shutil.which("launchctl"):
        raise SystemExit("the host memory sampler currently supports macOS only")
    plist_path = sampler_plist_path()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)], capture_output=True
    )
    plist_path.unlink(missing_ok=True)
    return {"removed": True, "label": SAMPLER_LABEL, "sample_preserved": str(host_metrics_path())}


def main() -> int:
    args = make_parser().parse_args()

    if args.command == "_local-worker":
        return run_local_worker(args.job_id)

    if args.command == "mcp-server":
        return run_mcp_server()

    if args.command == "queue":
        if args.history_limit < 0 or args.history_limit > 100:
            raise SystemExit("history-limit must be between 0 and 100")
        snapshot = queue_snapshot(args.history_limit)
        print_json(snapshot) if args.json else human_queue(snapshot)
        return 0

    if args.command == "cleanup":
        print_json(cleanup_stale())
        return 0

    if args.command == "dashboard":
        if args.port < 0 or args.port > 65535:
            raise SystemExit("port must be between 0 and 65535")
        return run_dashboard(args.host, args.port)

    if args.command == "sample-memory":
        sample = write_host_sample()
        if not args.quiet:
            print_json(sample)
        return 0

    if args.command == "sampler-install":
        print_json(sampler_install())
        return 0

    if args.command == "sampler-status":
        print_json(sampler_status())
        return 0

    if args.command == "sampler-remove":
        print_json(sampler_remove())
        return 0

    if args.command == "trust-status":
        print_json(trust_status())
        return 0

    if args.command == "trust-approve":
        if (
            args.max_cpu < 1 or args.max_memory_mb < 256 or args.max_runtime_seconds < 60
            or not math.isfinite(args.max_estimated_cost_usd)
            or args.max_estimated_cost_usd <= 0 or args.expires_days < 1
        ):
            raise SystemExit("trust limits must be positive and runtime must be at least 60 seconds")
        value = approve_trust(
            Path(args.path).expanduser(), args.provider, args.source_root,
            args.allow_uncommitted, args.allow_private, args.max_cpu,
            args.max_memory_mb, args.max_runtime_seconds,
            args.max_estimated_cost_usd, args.expires_days,
        )
        print_json(value)
        return 0

    if args.command == "trust-revoke":
        print_json(revoke_trust(Path(args.path).expanduser()))
        return 0

    if args.command == "status":
        with locked_state() as (data, _):
            value = payload(system_metrics(), data["leases"])
        human_status(value) if args.human else print_json(value)
        return 0

    if args.command == "recommend":
        if args.max_count < 1:
            raise SystemExit("max-count must be at least 1")
        with locked_state() as (data, _):
            metrics = system_metrics()
            count = recommend_count(args.workload, args.max_count, metrics, data["leases"])
            value = {"workload": args.workload, "recommended_count": count, **payload(metrics, data["leases"])}
        print_json(value)
        return 0

    if args.command == "acquire":
        validate_count_and_ttl(args.count, args.ttl)
        code, value = acquire(args.workload, args.count, args.owner, args.ttl)
        print_json(value)
        return code

    if args.command == "release":
        code, value = release(args.token)
        print_json(value)
        return code

    if args.command == "renew":
        validate_count_and_ttl(1, args.ttl)
        code, value = renew(args.token, args.ttl)
        print_json(value)
        return code

    if args.command == "run":
        validate_count_and_ttl(args.count, args.ttl)
        if args.poll_seconds <= 0:
            raise SystemExit("poll-seconds must be greater than zero")
        command = args.remainder
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            raise SystemExit("run requires a command after --")
        code, value = acquire(args.workload, args.count, args.owner, args.ttl)
        if code:
            if args.queue:
                job = enqueue_local_job(
                    args.workload, args.count, args.owner, args.ttl, command, args.poll_seconds, value
                )
                print_json({
                    "executed": False,
                    "queued": True,
                    "reason": "local capacity is unavailable; the request will start automatically when admitted",
                    "job": public_local_job(job),
                    "status_command": f"elsewhere job-status {job['id']}",
                    "logs_command": f"elsewhere job-logs {job['id']}",
                    "cancel_command": f"elsewhere job-cancel {job['id']}",
                })
                return 0
            print_json(value)
            return code
        token = value["token"]
        try:
            with keep_lease_alive(token, args.ttl):
                return subprocess.run(command).returncode
        finally:
            release(token)

    if args.command == "init":
        path = Path(args.path).expanduser()
        if path.exists() and not args.force:
            raise SystemExit(f"refusing to overwrite existing config: {path}")
        fly_app = args.fly_app
        tigris_bucket = args.tigris_bucket
        azure_subscription = args.azure_subscription
        azure_resource_group = args.azure_resource_group
        azure_storage_account = args.azure_storage_account
        if args.provider == "fly":
            fly_app = prompt_init_value(
                fly_app, "Fly app", "create a dedicated empty app and pass --fly-app"
            )
            tigris_bucket = prompt_init_value(
                tigris_bucket,
                "Tigris bucket",
                "run `fly storage create`, export its AWS credentials, and pass --tigris-bucket",
            )
        else:
            azure_subscription = prompt_init_value(
                azure_subscription, "Azure subscription", "pass --azure-subscription"
            )
            azure_resource_group = prompt_init_value(
                azure_resource_group, "Azure resource group", "pass --azure-resource-group"
            )
            azure_storage_account = prompt_init_value(
                azure_storage_account, "Azure storage account", "pass --azure-storage-account"
            )
        config = initial_config(
            default_config(),
            provider=args.provider,
            fly_app=fly_app,
            fly_org=args.fly_org,
            fly_region=args.fly_region,
            fly_region_fallbacks=args.fly_region_fallback,
            tigris_bucket=tigris_bucket,
            azure_subscription=azure_subscription,
            azure_resource_group=azure_resource_group,
            azure_location=args.azure_location,
            azure_storage_account=azure_storage_account,
        )
        missing = required_init_values(config)
        if missing:
            raise SystemExit("configuration is incomplete: " + ", ".join(missing))
        save_config(config, path)
        source_root = str(Path(args.source_root).expanduser().resolve())
        trust_command = " ".join([
            "elsewhere", "trust-approve", "--path", shlex.quote(str(path)),
            "--provider", args.provider, "--source-root", shlex.quote(source_root),
            "--allow-private",
            "--max-cpu", "4", "--max-memory-mb", "8192",
            "--max-runtime-seconds", "3600", "--max-estimated-cost-usd", "5",
        ])
        value = {
            "created": str(path),
            "provider": args.provider,
            "artifact_store": config["artifact_store"]["provider"],
            "next": [
                "elsewhere doctor --source-path " + shlex.quote(source_root),
                trust_command,
                "elsewhere route --workload test --execution remote --provider "
                + args.provider + " --image curlimages/curl:8.10.1 --source-path "
                + shlex.quote(source_root)
                + " --command 'printf elsewhere-ok' --cpu 1 --memory-mb 512"
                + " --max-runtime-seconds 600 --estimated-cost-usd 0.05",
            ],
        }
        print_json(value) if args.json else human_init(value)
        return 0

    if args.command == "doctor":
        selected = config_path()
        config = load_config()
        value = doctor_report(
            config,
            selected,
            lambda provider: provider_ready(provider, config),
            lambda: artifact_store_ready(config),
            trust_status(config),
            system_metrics(),
            source_path=args.source_path,
            source_allowed=path_is_allowed,
            source_inspect=source_state,
        )
        print_json(value) if args.json else human_doctor(value)
        return 0 if value["ready_for_planning"] else 1

    if args.command == "providers":
        config = load_config()
        values = {}
        for provider in config["providers"]:
            ready, reason = provider_ready(provider, config)
            values[provider] = {
                "adapter_installed": provider in SUPPORTED_PROVIDERS,
                "ready": ready,
                "reason": reason,
                "config": config["providers"][provider],
            }
        print_json({
            "routing": config["routing"],
            "providers": values,
            "trust": trust_status(config),
            "extension_contract": {
                "input": ["image", "command", "cpu", "memory_mb", "git_url", "git_ref", "result_paths"],
                "lifecycle": ["dispatch", "status", "logs", "results", "cancel", "cleanup"],
            },
        })
        return 0

    if args.command == "init-config":
        path = Path(args.path).expanduser()
        if path.exists() and not args.force:
            raise SystemExit(f"refusing to overwrite existing config: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(default_config(), indent=2, sort_keys=True) + "\n")
        print_json({"created": str(path), "default_provider": "fly", "artifact_store": "tigris"})
        return 0

    if args.command == "dispatch":
        if args.cpu < 1 or args.memory_mb < 256:
            raise SystemExit("dispatch requires cpu >= 1 and memory-mb >= 256")
        if (
            args.max_runtime_seconds < 60 or not math.isfinite(args.estimated_cost_usd)
            or args.estimated_cost_usd < 0
        ):
            raise SystemExit("max-runtime-seconds must be at least 60 and estimated cost cannot be negative")
        job, plan = build_dispatch_plan(
            args.workload, args.provider, args.image, args.workload_command,
            args.cpu, args.memory_mb, args.git_url, args.git_ref, args.source_path,
            args.max_runtime_seconds, args.estimated_cost_usd,
            args.result_path,
        )
        if not args.execute:
            print_json({
                "executed": False,
                "job": job,
                "plan": plan,
                "next": "review shell_preview and trust, then repeat with --execute",
            })
            return 0
        config = load_config()
        require_trust(job, plan, config, args.approval_receipt)
        job, plan = attach_execution_artifacts(
            job, args.workload_command, args.max_runtime_seconds, config, args.approval_receipt
        )
        code, value = execute_dispatch(job, plan, args.approval_receipt)
        print_json(value)
        return code

    if args.command == "route":
        if args.poll_seconds <= 0:
            raise SystemExit("poll-seconds must be greater than zero")
        if (
            args.max_runtime_seconds < 60 or not math.isfinite(args.estimated_cost_usd)
            or args.estimated_cost_usd < 0
        ):
            raise SystemExit("max-runtime-seconds must be at least 60 and estimated cost must be finite")
        decision = local_route_decision(args.workload)
        placement = decision["placement"] if args.execution == "auto" else args.execution
        automatic_placement = decision["placement"]
        decision["placement"] = placement
        decision["forced"] = args.execution != "auto"
        decision["automatic_placement"] = automatic_placement
        if decision["forced"]:
            decision["reason"] = f"caller explicitly selected {placement} execution"
        if placement == "local":
            if not args.execute:
                print_json({"executed": False, "decision": decision, "command": args.workload_command})
                return 0
            code, lease = acquire(args.workload, 1, args.owner, DEFAULT_TTL)
            if code:
                if args.queue:
                    job = enqueue_local_job(
                        args.workload,
                        1,
                        args.owner,
                        DEFAULT_TTL,
                        ["/bin/sh", "-lc", args.workload_command],
                        args.poll_seconds,
                        lease,
                    )
                    print_json({
                        "executed": False,
                        "queued": True,
                        "decision": decision,
                        "admission": lease,
                        "job": public_local_job(job),
                        "status_command": f"elsewhere job-status {job['id']}",
                    })
                    return 0
                print_json({"executed": False, "decision": decision, "admission": lease})
                return code
            try:
                result = subprocess.run(["/bin/sh", "-lc", args.workload_command], text=True, capture_output=True)
            finally:
                release(lease["token"])
            print_json({
                "executed": True,
                "decision": decision,
                "returncode": result.returncode,
                "stdout": result.stdout[-12000:],
                "stderr": result.stderr[-12000:],
            })
            return result.returncode

        if not args.image:
            raise SystemExit("remote placement requires --image")
        job, plan = build_dispatch_plan(
            args.workload, args.provider, args.image, args.workload_command,
            args.cpu, args.memory_mb, args.git_url, args.git_ref, args.source_path,
            args.max_runtime_seconds, args.estimated_cost_usd,
            args.result_path,
        )
        if not args.execute:
            print_json({"executed": False, "decision": decision, "job": job, "plan": plan})
            return 0
        config = load_config()
        require_trust(job, plan, config, args.approval_receipt)
        job, plan = attach_execution_artifacts(
            job, args.workload_command, args.max_runtime_seconds, config, args.approval_receipt
        )
        code, value = execute_dispatch(job, plan, args.approval_receipt)
        value["decision"] = decision
        print_json(value)
        return code

    if args.command in ("job-status", "job-logs", "job-results", "job-cancel", "job-cleanup"):
        return run_job_action(
            args.job_id,
            args.command.removeprefix("job-"),
            discard_results=bool(getattr(args, "discard_results", False)),
        )

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
