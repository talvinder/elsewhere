"""Provider contract used by Elsewhere's remote lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderObservation:
    state: str | None
    provider_id: str | None = None
    absent: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


class ComputeProvider(Protocol):
    name: str

    def ready(self, values: dict[str, Any]) -> tuple[bool, str]: ...

    def identity(self, values: dict[str, Any]) -> dict[str, str]: ...

    def regions(self, values: dict[str, Any]) -> list[str]: ...

    def build_plan(self, job: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]: ...

    def parse_submission(self, stdout: str, stderr: str, job: dict[str, Any]) -> str | None: ...

    def status_command(self, job: dict[str, Any]) -> list[str]: ...

    def parse_status(self, stdout: str, stderr: str, returncode: int, job: dict[str, Any]) -> ProviderObservation: ...

    def logs_command(self, job: dict[str, Any]) -> list[str]: ...

    def cancel_command(self, job: dict[str, Any]) -> list[str]: ...

    def cleanup_command(self, job: dict[str, Any]) -> list[str]: ...

    def classify_failure(self, stderr: str) -> str: ...

    def result_strategy(self, job: dict[str, Any]) -> str: ...
