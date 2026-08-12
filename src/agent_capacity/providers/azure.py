"""Azure Container Instances adapter."""

from __future__ import annotations

import json
import shlex
import shutil
from typing import Any

from agent_capacity.models import normalize_remote_state
from agent_capacity.providers.base import ProviderObservation


class AzureProvider:
    name = "azure"

    def ready(self, values: dict[str, Any]) -> tuple[bool, str]:
        if not values.get("enabled", False):
            return False, "disabled in config"
        if shutil.which("az") is None:
            return False, "Azure CLI is not installed"
        if not values.get("resource_group"):
            return False, "providers.azure.resource_group is required"
        return True, "ready"

    def identity(self, values: dict[str, Any]) -> dict[str, str]:
        return {
            "subscription": values.get("subscription", ""),
            "resource_group": values.get("resource_group", ""),
        }

    def regions(self, values: dict[str, Any]) -> list[str]:
        return [values.get("location", "")]

    def _subscription(self, job: dict[str, Any]) -> list[str]:
        subscription = job["plan"]["provider_config"].get("subscription")
        return ["--subscription", subscription] if subscription else []

    def build_plan(self, job: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        memory_gb = round(job["memory_mb"] / 1024, 1)
        command = [
            "az", "container", "create",
            "--resource-group", values["resource_group"],
            "--name", job["name"],
            "--image", job["image"],
            "--cpu", str(job["cpu"]),
            "--memory", str(memory_gb),
            "--location", values.get("location", "centralindia"),
            "--os-type", "Linux",
            "--restart-policy", "Never",
            "--command-line", f"/bin/sh -lc {shlex.quote(job['remote_command'])}",
            "--no-wait", "--only-show-errors", "--output", "json",
        ]
        if values.get("subscription"):
            command.extend(["--subscription", values["subscription"]])
        return {
            "provider": self.name,
            "command": command,
            "status_command": [
                "az", "container", "show", "--resource-group", values["resource_group"],
                "--name", job["name"], "--output", "json",
            ],
            "logs_command": [
                "az", "container", "logs", "--resource-group", values["resource_group"],
                "--name", job["name"],
            ],
            "cleanup": "Elsewhere deletes the container group and verifies its absence",
            "provider_config": {
                "resource_group": values["resource_group"],
                "location": values.get("location", "centralindia"),
                "subscription": values.get("subscription"),
            },
        }

    def parse_submission(self, stdout: str, stderr: str, job: dict[str, Any]) -> str | None:
        try:
            result = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            result = {}
        return result.get("id") or job["name"]

    def status_command(self, job: dict[str, Any]) -> list[str]:
        group = job["plan"]["provider_config"]["resource_group"]
        return [
            "az", "container", "show", "--resource-group", group,
            "--name", job["name"], "--output", "json", *self._subscription(job),
        ]

    def parse_status(self, stdout: str, stderr: str, returncode: int, job: dict[str, Any]) -> ProviderObservation:
        if returncode:
            absent = "ResourceNotFound" in stderr or "could not be found" in stderr
            evidence = (
                {"provider_state": "absent"}
                if absent else {"error": "Azure status failed", "returncode": returncode}
            )
            return ProviderObservation(
                None, job.get("provider_id") or job["name"], absent=absent, evidence=evidence
            )
        try:
            result = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            return ProviderObservation(None, job.get("provider_id") or job["name"], evidence={"error": "invalid Azure JSON"})
        group_state = (result.get("instanceView") or {}).get("state")
        provisioning_state = result.get("provisioningState")
        containers = result.get("containers") or []
        current = ((containers[0].get("instanceView") or {}).get("currentState") or {}) if containers else {}
        raw_state = current.get("state") or group_state
        if raw_state:
            state = normalize_remote_state(raw_state)
        elif str(provisioning_state).lower() == "failed":
            state = "failed"
        else:
            # ACI provisioning success means the group exists, not that the workload exited.
            state = "queued"
        exit_code = current.get("exitCode")
        if state == "completed" and exit_code is not None:
            state = "succeeded" if int(exit_code) == 0 else "failed"
        return ProviderObservation(
            state,
            result.get("id") or job.get("provider_id") or job["name"],
            evidence={
                "provider_state": raw_state,
                "provisioning_state": provisioning_state,
                "exit_code": exit_code,
            },
        )

    def logs_command(self, job: dict[str, Any]) -> list[str]:
        group = job["plan"]["provider_config"]["resource_group"]
        return [
            "az", "container", "logs", "--resource-group", group,
            "--name", job["name"], *self._subscription(job),
        ]

    def cancel_command(self, job: dict[str, Any]) -> list[str]:
        return self.cleanup_command(job)

    def cleanup_command(self, job: dict[str, Any]) -> list[str]:
        group = job["plan"]["provider_config"]["resource_group"]
        return [
            "az", "container", "delete", "--resource-group", group,
            "--name", job["name"], "--yes", "--only-show-errors", "--output", "none",
            *self._subscription(job),
        ]

    def classify_failure(self, stderr: str) -> str:
        retryable = (
            "AllocationFailed", "TooManyRequests", "RegistryErrorResponse", "timeout",
            "temporarily unavailable", "please retry later",
        )
        return "retryable" if any(value.lower() in stderr.lower() for value in retryable) else "terminal"

    def result_strategy(self, job: dict[str, Any]) -> str:
        return "remote-upload"
