"""Provider-neutral workspace intent, capabilities, and approval boundaries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

INTENTS = ("fresh", "project", "isolated")
RETENTION = ("keep", "sleep", "checkpoint", "delete")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Capabilities:
    persistent_files: bool = False
    sessions: bool = False
    snapshots: bool = False
    checkpoints: bool = False
    native_fork: bool = False
    network_policy: bool = False
    retention: tuple[str, ...] = ()


class WorkspaceProvider(Protocol):
    name: str
    capabilities: Capabilities

    def ready(self) -> tuple[bool, str]: ...
    def identity(self, values: dict) -> dict: ...
    def resources(self) -> dict: ...
    def get(self, name: str) -> dict | None: ...
    def create(self, name: str) -> dict: ...
    def delete(self, name: str) -> None: ...
    def read(self, name: str, path: str) -> bytes: ...
    def write(self, name: str, path: str, data: bytes) -> None: ...
    def start(self, name: str, argv: list[str], runtime: int) -> str: ...
    def cancel(self, name: str, session: str) -> None: ...
    def checkpoint(self, name: str, comment: str) -> str: ...
    def policy(self, name: str) -> dict: ...
    def set_policy(self, name: str, policy: dict) -> None: ...
    def checkpoints(self, name: str) -> list[dict]: ...
    def verify_connectors(self, expected: list[dict]) -> None: ...
    def activity_commands(self, job_id: str, runtime: int) -> dict: ...


def validate_request(request: dict, capabilities: Capabilities) -> None:
    if not isinstance(request, dict):
        raise ValueError("workspace request must be an object")
    allowed = {
        "intent",
        "derivation",
        "project",
        "parent",
        "retention",
        "retention_seconds",
        "network_policy",
        "connectors",
        "resources",
    }
    if set(request) - allowed:
        raise ValueError("unknown workspace request fields")
    intent = request.get("intent")
    if intent not in INTENTS:
        raise ValueError("workspace intent must be fresh, project, or isolated")
    if not capabilities.persistent_files or not capabilities.sessions:
        raise ValueError("provider lacks continuing workspace capabilities")
    derivation = request.get("derivation", "snapshot")
    if derivation not in ("snapshot", "native-fork"):
        raise ValueError("unknown workspace derivation")
    if derivation == "native-fork" and (
        intent != "isolated" or not capabilities.native_fork
    ):
        raise ValueError(
            "native checkpoint fork is unsupported; snapshot substitution is forbidden"
        )
    if derivation == "snapshot" and not capabilities.snapshots:
        raise ValueError("provider cannot seed repository snapshots")
    project = request.get("project")
    if (intent == "project") != bool(project):
        raise ValueError(
            "project intent requires an exact project identity; other intents cannot select a project"
        )
    for identity in (project, request.get("parent")):
        if identity is not None and (
            not isinstance(identity, dict)
            or not identity.get("name")
            or not identity.get("id")
        ):
            raise ValueError("workspace references require name and immutable id")
    if request.get("parent") and intent != "isolated":
        raise ValueError("parent lineage belongs only to an isolated task")
    if derivation == "native-fork" and not request.get("parent", {}).get("checkpoint"):
        raise ValueError("native fork requires an exact parent checkpoint")
    retention = request.get("retention")
    if retention not in capabilities.retention:
        raise ValueError("provider does not support the requested retention action")
    if retention == "checkpoint" and not capabilities.checkpoints:
        raise ValueError("provider cannot checkpoint")
    if intent == "project" and retention in ("delete", "checkpoint"):
        raise ValueError("task cleanup cannot delete or snapshot a shared project")
    seconds = request.get("retention_seconds")
    if (
        type(seconds) is not int
        or not 0 <= seconds <= 604800
        or (retention != "delete" and seconds == 0)
    ):
        raise ValueError(
            "retention requires an explicit bounded duration of at most seven days"
        )
    if not capabilities.network_policy or not isinstance(
        request.get("network_policy"), dict
    ):
        raise ValueError("an explicit supported network policy is required")
    connectors = request.get("connectors")
    if not isinstance(connectors, list) or any(
        not isinstance(item, dict)
        or set(item) != {"id", "provider", "access_policy"}
        or not isinstance(item["id"], str)
        or not isinstance(item["provider"], str)
        or not isinstance(item["access_policy"], dict)
        for item in connectors
    ):
        raise ValueError(
            "connectors require explicit identities and complete access policies"
        )
    if not isinstance(request.get("resources"), dict):
        raise ValueError("explicit provider resource semantics must be approved")


def choose_workspace_provider(
    request: dict, candidates: list[WorkspaceProvider]
) -> WorkspaceProvider:
    reasons = []
    for provider in candidates:
        try:
            validate_request(request, provider.capabilities)
            if request["resources"] != provider.resources():
                raise ValueError(
                    "requested resource semantics differ from this provider"
                )
            return provider
        except ValueError as error:
            reasons.append(f"{provider.name}: {error}")
    raise ValueError("no compatible workspace provider: " + "; ".join(reasons))


def approval_boundary(job: dict, identity: dict) -> dict:
    return {
        "provider": job["provider"],
        "identity": identity,
        "source_path": job["source_path"],
        "workspace": job["workspace"],
        "command_sha256": fingerprint(job["command"]),
        "max_runtime_seconds": job["max_runtime_seconds"],
        "estimated_cost_usd": job["estimated_cost_usd"],
        "result_paths": job["result_paths"],
    }
