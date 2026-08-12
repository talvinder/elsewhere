"""Guided configuration and read-only first-run diagnostics."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable


def initial_config(
    base: dict[str, Any],
    *,
    provider: str,
    fly_app: str = "",
    fly_org: str = "",
    fly_region: str = "bom",
    fly_region_fallbacks: list[str] | None = None,
    tigris_bucket: str = "",
    azure_subscription: str = "",
    azure_resource_group: str = "",
    azure_location: str = "centralindia",
    azure_storage_account: str = "",
) -> dict[str, Any]:
    config = deepcopy(base)
    config["routing"]["default"] = provider
    config["routing"]["fallbacks"] = []
    config["providers"]["fly"].update({
        "enabled": provider == "fly",
        "app": fly_app,
        "org": fly_org,
        "region": fly_region,
        "region_fallbacks": list(dict.fromkeys(fly_region_fallbacks or [])),
    })
    config["providers"]["azure"].update({
        "enabled": provider == "azure",
        "subscription": azure_subscription,
        "resource_group": azure_resource_group,
        "location": azure_location,
    })
    if provider == "fly":
        config["artifact_store"].update({
            "provider": "tigris",
            "bucket": tigris_bucket,
            "endpoint": "https://t3.storage.dev",
            "region": "auto",
            "addressing_style": "virtual",
            "presign_ttl_minutes": 60,
        })
    else:
        config["artifact_store"].update({
            "provider": "azure-blob",
            "account": azure_storage_account,
            "container": "elsewhere-artifacts",
            "subscription": azure_subscription,
            "sas_ttl_minutes": 60,
        })
    config["trust"] = deepcopy(base["trust"])
    config["trust"]["inherit_global"] = False
    return config


def required_init_values(config: dict[str, Any]) -> list[str]:
    provider = config["routing"]["default"]
    missing: list[str] = []
    if provider == "fly":
        if not config["providers"]["fly"].get("app"):
            missing.append("Fly app")
        if not config["artifact_store"].get("bucket"):
            missing.append("Tigris bucket")
    elif provider == "azure":
        if not config["providers"]["azure"].get("subscription"):
            missing.append("Azure subscription")
        if not config["providers"]["azure"].get("resource_group"):
            missing.append("Azure resource group")
        if not config["artifact_store"].get("account"):
            missing.append("Azure storage account")
    return missing


def doctor_report(
    config: dict[str, Any],
    selected_path: Path | None,
    provider_check: Callable[[str], tuple[bool, str]],
    artifact_check: Callable[[], tuple[bool, str]],
    trust: dict[str, Any],
    metrics: dict[str, Any],
    source_path: str | None = None,
    source_allowed: Callable[[str, list[str]], bool] | None = None,
    source_inspect: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, message: str, next_action: str = "") -> None:
        value = {"name": name, "status": status, "message": message}
        if next_action:
            value["next"] = next_action
        checks.append(value)

    if sys.version_info >= (3, 11):
        add("runtime", "pass", f"Python {sys.version_info.major}.{sys.version_info.minor} is supported")
    else:
        add("runtime", "fail", "Python 3.11 or newer is required", "install Python 3.11+ and reinstall Elsewhere")

    if selected_path and selected_path.exists():
        add("config", "pass", f"configuration found at {selected_path}")
    else:
        add("config", "fail", "no Elsewhere configuration was found", "run `elsewhere init`")

    provider = str(config.get("routing", {}).get("default", ""))
    provider_ok, provider_reason = provider_check(provider)
    add(
        "compute",
        "pass" if provider_ok else "fail",
        f"{provider}: {provider_reason}",
        "repair the provider configuration and authentication" if not provider_ok else "",
    )

    artifact_ok, artifact_reason = artifact_check()
    artifact_name = str(config.get("artifact_store", {}).get("provider", ""))
    add(
        "artifacts",
        "pass" if artifact_ok else "fail",
        f"{artifact_name}: {artifact_reason}",
        "repair artifact storage before exporting source" if not artifact_ok else "",
    )

    if trust.get("valid"):
        add("trust", "pass", "an unexpired execution boundary is approved")
    else:
        trust_reason = "; ".join(trust.get("reasons", []))
        add(
            "trust",
            "warn",
            trust_reason or "planning is available, but remote execution is not approved",
            "review the generated config, then run `elsewhere trust-approve --help`",
        )

    sensing = bool(metrics.get("sensing_available"))
    add(
        "local sensing",
        "pass" if sensing else "warn",
        "local capacity signals are available" if sensing else "local sensing is unavailable; remote execution still works",
        "run `elsewhere sampler-install` on macOS" if not sensing else "",
    )

    if source_path and source_allowed:
        source_policy = trust.get("source", {})
        if not trust.get("configured") and not source_policy:
            add(
                "source boundary",
                "warn",
                "source export has not been approved yet",
                "approve the intended source root and source state explicitly",
            )
            failures = sum(item["status"] == "fail" for item in checks)
            warnings = sum(item["status"] == "warn" for item in checks)
            return {
                "ready_for_planning": failures == 0,
                "ready_for_execution": False,
                "failures": failures,
                "warnings": warnings,
                "checks": checks,
            }
        roots = source_policy.get("allowed_roots", [])
        allowed = source_allowed(source_path, roots)
        source_value = source_inspect(source_path) if source_inspect else {}
        private_allowed = source_policy.get("allow_private") is True
        dirty_allowed = (
            source_value.get("dirty") is not True
            or source_policy.get("allow_uncommitted") is True
        )
        source_ready = allowed and private_allowed and dirty_allowed
        reasons = []
        if not allowed:
            reasons.append("source is outside the approved roots")
        if not private_allowed:
            reasons.append("private source export is not approved")
        if not dirty_allowed:
            reasons.append("uncommitted or unversioned source export is not approved")
        add(
            "source boundary",
            "pass" if source_ready else "fail",
            "source is inside the approved execution boundary"
            if source_ready else "; ".join(reasons),
            "approve the intended source root and source state explicitly"
            if not source_ready else "",
        )

    failures = sum(item["status"] == "fail" for item in checks)
    warnings = sum(item["status"] == "warn" for item in checks)
    return {
        "ready_for_planning": failures == 0,
        "ready_for_execution": failures == 0 and bool(trust.get("valid")),
        "failures": failures,
        "warnings": warnings,
        "checks": checks,
    }
