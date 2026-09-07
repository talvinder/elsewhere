<!-- agent-capacity:start -->
## Elsewhere placement and shared capacity

Before a portable build or substantial test run, load the Elsewhere skill and
assess placement with `elsewhere_plan` or a dry `elsewhere route`, even when the
user has not mentioned Elsewhere. A local capacity denial or queued job also
requires a remote assessment before waiting. Explain host-specific constraints
that require local execution. Ordinary reads and small commands need no placement
overhead. Never promise faster completion without evidence.

Before launching local agents, browser automation, builds, or parallel test workers,
reserve shared capacity with `~/.local/bin/agent-capacity acquire`. Launch only when
`allowed` is true; respect the returned count, and release owned leases when done.
For one local heavy command use `elsewhere run`. Never split a request to bypass
denial, release another owner's work, or keep a lease while waiting for user input.

Review remote plans against the active trust receipt. Reuse matching execution
authorization; never silently broaden source, destination, cost, or runtime limits.
CLI cloud execution requires explicit `--execute`. Verify cancellation of a
superseded task-owned queued job before submitting its replacement remotely.

Retain remote job IDs and follow them through completion without a user reminder.
Use bounded `elsewhere_job_wait` calls when available, otherwise inspect status.
Continue independent work and report meaningful changes, avoiding repeated notices
for the same queue event. Recover and inspect verified results, fix failures within
scope, then verify authorized cleanup after preserving results. Finish with the
source fingerprint, outcome, result location, and cleanup status.

Use the installed Elsewhere skill for the full provider-neutral workflow. The
compatibility `agent-capacity` skill follows the same placement-first sequence.
<!-- agent-capacity:end -->
