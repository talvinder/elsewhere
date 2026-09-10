"""Deterministic, offline comparison of caller-supplied execution estimates."""

import math


def number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite nonnegative number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be a finite nonnegative number")
    return value


def compare(spec):
    """Rank eligible candidates; never authorize export or provision resources."""
    if not isinstance(spec, dict) or spec.get("version") != 1:
        raise ValueError("comparison version must be 1")
    candidates = spec.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("candidates must be a nonempty list")
    deadline = number(spec["deadline_seconds"], "deadline_seconds")
    budget = number(spec["budget_usd"], "budget_usd")
    rows, seen = [], set()
    fields = (
        "runtime_seconds", "setup_seconds", "queue_seconds", "average_cpus",
        "average_memory_gb", "fixed_hour_usd", "cpu_hour_usd", "gb_hour_usd",
        "transfer_usd", "retained_storage_usd",
    )
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("each candidate must be an object")
        identity = candidate.get("id")
        if not isinstance(identity, str) or not identity.strip() or identity in seen:
            raise ValueError("candidate ids must be nonempty and unique")
        seen.add(identity)
        reasons = candidate.get("ineligible_reasons")
        if not isinstance(reasons, list) or any(not isinstance(x, str) or not x.strip() for x in reasons):
            raise ValueError("ineligible_reasons must explicitly list unmet requirements")
        values = {key: number(candidate[key], key) for key in fields}
        # Setup is billed at the same declared utilization as execution.
        hours = (values["runtime_seconds"] + values["setup_seconds"]) / 3600
        compute = hours * (values["fixed_hour_usd"] + values["average_cpus"] * values["cpu_hour_usd"]
                           + values["average_memory_gb"] * values["gb_hour_usd"])
        total = compute + values["transfer_usd"] + values["retained_storage_usd"]
        elapsed = values["runtime_seconds"] + values["setup_seconds"] + values["queue_seconds"]
        if not math.isfinite(total) or not math.isfinite(elapsed):
            raise ValueError("estimate overflow")
        reasons = list(reasons)
        if elapsed > deadline:
            reasons.append("completion deadline exceeded")
        if total > budget:
            reasons.append("estimated budget exceeded")
        rows.append({"id": identity, "eligible": not reasons, "ineligible_reasons": reasons,
                     "estimated_cost_usd": total, "compute_usd": compute,
                     "estimated_completion_seconds": elapsed})
    eligible = sorted((r for r in rows if r["eligible"]),
                      key=lambda r: (r["estimated_cost_usd"], r["estimated_completion_seconds"], r["id"]))
    return {"version": 1, "executed": False, "authorization_granted": False,
            "recommendation": eligible[0]["id"] if eligible else None,
            "basis": "caller-supplied rates, utilization, and eligibility; trust must be checked at dispatch",
            "candidates": rows}
