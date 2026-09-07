"""Fly Machines adapter."""

from __future__ import annotations

import json
import re
import shutil
from typing import Any

from agent_capacity.models import normalize_remote_state
from agent_capacity.providers.base import ProviderObservation


class FlyProvider:
    name = "fly"

    def ready(self, values: dict[str, Any]) -> tuple[bool, str]:
        if not values.get("enabled", False):
            return False, "disabled in config"
        if shutil.which("fly") is None:
            return False, "fly CLI is not installed"
        if not values.get("app"):
            return False, "providers.fly.app is required"
        return True, "ready"

    def identity(self, values: dict[str, Any]) -> dict[str, str]:
        return {"app": values.get("app", ""), "org": values.get("org", "")}

    def regions(self, values: dict[str, Any]) -> list[str]:
        return list(dict.fromkeys([values.get("region", ""), *values.get("region_fallbacks", [])]))

    def build_plan(self, job: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        command = [
            "fly", "machine", "run", job["image"],
            "--app", values["app"],
            "--name", job["name"],
            "--region", values.get("region", "bom"),
            "--vm-cpu-kind", values.get("cpu_kind", "shared"),
            "--vm-cpus", str(job["cpu"]),
            "--vm-memory", f"{job['memory_mb']}mb",
            "--restart", "no",
            "--detach",
            "--entrypoint", "/bin/sh",
            "--", "-lc", job["remote_command"],
        ]
        return {
            "provider": self.name,
            "command": command,
            "status_command": ["fly", "machine", "list", "--app", values["app"], "--json"],
            "logs_command": "available after Fly returns the Machine ID",
            "cleanup": "Elsewhere explicitly destroys the retained Machine and verifies its absence",
            "provider_config": {
                "app": values["app"],
                "region": values.get("region", "bom"),
                "region_fallbacks": values.get("region_fallbacks", []),
            },
        }

    def parse_submission(self, stdout: str, stderr: str, job: dict[str, Any]) -> str | None:
        # Prefer the Machine ID from structured JSON when Fly emits it; only then fall
        # back to scraping human text, which is more sensitive to CLI format changes.
        for blob in (stdout, stderr):
            machine_id = self._machine_id_from_json(blob)
            if machine_id:
                return machine_id
        text = f"{stdout}\n{stderr}"
        patterns = (
            r"\bMachine\s+([0-9a-f]{12,32})\b",
            r"\bmachine(?:\s+id)?[=: ]+([0-9a-f]{12,32})\b",
            r'"id"\s*:\s*"([0-9a-f]{12,32})"',
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _machine_id_from_json(blob: str | None) -> str | None:
        text = (blob or "").strip()
        if not text or text[0] not in "[{":
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        items = data if isinstance(data, list) else [data]
        for item in items:
            candidate = item.get("id") if isinstance(item, dict) else None
            if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{12,32}", candidate):
                return candidate
        return None

    def status_command(self, job: dict[str, Any]) -> list[str]:
        app = job["plan"]["provider_config"]["app"]
        if job.get("provider_id"):
            return ["fly", "machine", "status", job["provider_id"], "--app", app]
        return ["fly", "machine", "list", "--app", app, "--json"]

    def parse_status(self, stdout: str, stderr: str, returncode: int, job: dict[str, Any]) -> ProviderObservation:
        if returncode:
            absent = "could not be found" in stderr.lower() or "not found" in stderr.lower()
            return ProviderObservation(
                None, job.get("provider_id"), absent=absent,
                evidence=(
                    {"provider_state": "absent"}
                    if absent else {"error": "Fly status failed", "returncode": returncode}
                ),
            )
        if job.get("provider_id") and re.search(r"^State:\s*", stdout, re.MULTILINE):
            state_match = re.search(r"^State:\s*(\S+)", stdout, re.MULTILINE)
            region_match = re.search(r"^\s*Region\s*[│:]\s*(\S+)", stdout, re.MULTILINE)
            exit_match = re.search(r"\bexit_code=(\d+)\b", stdout)
            raw_state = state_match.group(1) if state_match else None
            state = normalize_remote_state(raw_state)
            exit_code = int(exit_match.group(1)) if exit_match else None
            absent = (raw_state or "").lower() == "destroyed"
            if state == "completed" and exit_code is not None:
                state = "succeeded" if exit_code == 0 else "failed"
            return ProviderObservation(
                state, job["provider_id"], absent=absent,
                evidence={
                    "provider_state": raw_state,
                    "region": region_match.group(1) if region_match else None,
                    "exit_code": exit_code,
                },
            )
        try:
            machines = json.loads(stdout or "[]")
        except json.JSONDecodeError:
            return ProviderObservation(None, job.get("provider_id"), evidence={"error": "invalid Fly JSON"})
        provider_id = job.get("provider_id")
        # Match strictly by the dispatched Machine ID once it is known. Falling back to
        # the job name here would let a stale machine reused from a prior run in the same
        # Fly app be mistaken for this job's machine and report the wrong state.
        if provider_id:
            match = next((item for item in machines if item.get("id") == provider_id), None)
        else:
            match = next((item for item in machines if item.get("name") == job.get("name")), None)
        if not match:
            return ProviderObservation(None, provider_id, absent=True, evidence={"machine_count": len(machines)})
        raw_state = match.get("state") or match.get("status")
        return ProviderObservation(
            normalize_remote_state(raw_state),
            str(match.get("id") or provider_id or "") or None,
            evidence={"provider_state": raw_state, "region": match.get("region")},
        )

    def _machine_command(self, job: dict[str, Any], verb: str) -> list[str]:
        provider_id = job.get("provider_id")
        if not provider_id:
            raise ValueError("Fly Machine ID is not known; refresh job status first")
        app = job["plan"]["provider_config"]["app"]
        return ["fly", "machine", verb, provider_id, "--app", app, "--force"]

    def logs_command(self, job: dict[str, Any]) -> list[str]:
        provider_id = job.get("provider_id")
        if not provider_id:
            raise ValueError("Fly Machine ID is not known; refresh job status first")
        app = job["plan"]["provider_config"]["app"]
        return ["fly", "logs", "--app", app, "--machine", provider_id, "--json", "--no-tail"]

    def cancel_command(self, job: dict[str, Any]) -> list[str]:
        return self._machine_command(job, "destroy")

    def cleanup_command(self, job: dict[str, Any]) -> list[str]:
        return self._machine_command(job, "destroy")

    def classify_failure(self, stderr: str) -> str:
        # A retired region cannot recover on retry in place, but another configured
        # region can accept the job. The dispatcher rechecks trust on each fallback.
        if re.search(r"region\s+[a-z0-9-]+\s+is deprecated and cannot have new resources provisioned", stderr, re.IGNORECASE):
            return "retryable"
        retryable = ("no capacity available", "RegistryErrorResponse", "timeout", "temporarily unavailable")
        return "retryable" if any(value.lower() in stderr.lower() for value in retryable) else "terminal"

    def result_strategy(self, job: dict[str, Any]) -> str:
        return "remote-upload"
