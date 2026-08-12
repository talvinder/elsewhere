"""Shared lifecycle language for local and remote Elsewhere jobs."""

from __future__ import annotations

REMOTE_ACTIVE_STATES = {
    "planned",
    "submitting",
    "submission_uncertain",
    "submitted",
    "queued",
    "running",
    "cancelling",
    "cleaning",
}
REMOTE_TERMINAL_STATES = {
    "succeeded",
    "failed",
    "cancelled",
    "submission_failed",
    "completed",
    "cleaned",
    "cleanup_failed",
}


def normalize_remote_state(value: str | None) -> str | None:
    """Return Elsewhere's provider-neutral state for common provider values."""
    if not value:
        return None
    normalized = value.strip().lower().replace(" ", "_")
    aliases = {
        "created": "queued",
        "creating": "queued",
        "pending": "queued",
        "starting": "queued",
        "waiting": "queued",
        "started": "running",
        "executing": "running",
        "stopping": "running",
        "stopped": "completed",
        "terminated": "completed",
        "destroyed": "completed",
        "complete": "completed",
        "successful": "succeeded",
        "success": "succeeded",
        "error": "failed",
    }
    return aliases.get(normalized, normalized)


def should_accept_remote_transition(current: str | None, observed: str | None) -> bool:
    if not observed or observed == current:
        return False
    if current in REMOTE_TERMINAL_STATES:
        return observed == "cleaning" or (current == "cleanup_failed" and observed == "cleaning")
    order = {
        "planned": 0, "submitting": 1, "submission_uncertain": 2,
        "submitted": 2, "queued": 3, "running": 4,
    }
    if current in order and observed in order:
        return order[observed] >= order[current]
    return True
