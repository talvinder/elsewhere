---
name: elsewhere
description: Assess placement before portable builds, substantial tests, and capacity waits; manage local or approved cloud execution through verified results and cleanup.
---

# Elsewhere

Elsewhere decides whether work can run safely here or should continue somewhere
else. The user should not need to choose infrastructure unless they want to.

## Route work

Before a portable build or substantial test run, make a dry placement decision even
when the developer has not mentioned Elsewhere. A denied reservation or local queue
is also a trigger to assess remote execution before waiting. Explain host-dependent
work that must stay local. Do not duplicate a queued job: verify cancellation of your
own superseded job before starting its replacement.

Make a dry placement decision first:

```sh
elsewhere route \
  --workload build \
  --image node:22-bookworm \
  --source-path . \
  --command "npm ci && npm run build"
```

Review location, reason, CPU, memory, source boundary, cost implications, and cleanup.
Use `--execute` only after the plan is acceptable. Use `--execution remote` only
when the user deliberately wants work off the device.

Before remote execution, inspect `elsewhere trust-status`. The plan must report
`trust.allowed: true`; packaging and dispatch are denied when the account, region,
source root, private/uncommitted permission, resource limits, runtime, estimated
cost, or approval receipt differs from the active contract.

## Protect local capacity

Reserve shared capacity before spawning agents or parallel workers:

```sh
elsewhere acquire --workload parallel-agent --count 2 --owner "codex:task"
```

Launch only when `allowed` is true. Release the returned token when work finishes:

```sh
elsewhere release TOKEN
```

For one heavy local command, prefer automatic release:

```sh
elsewhere run --workload test --owner "claude:tests" -- npm test
```

`run` queues by default when local admission is temporarily denied. Keep the
returned job ID and use `job-status` or `job-logs`; the command starts automatically
when capacity returns. Use `--no-queue` only for an explicit fail-fast workflow.

Never split a request to bypass a denial. Reduce concurrency, run sequentially, or
route the workload remotely.

## Complete the developer task

Retain every submitted job ID, continue independent work, and check status without
a user reminder. Recover verified results, inspect failures, and fix and verify the
change within the original scope. Clean up completed task-owned remote resources
after preserving results when lifecycle cleanup is authorized. Report the source
fingerprint, exit code, result location, duration, estimated cost, and cleanup.
Do not repeat unchanged queue suggestions; retain their event_key. An existing
matching execution approval does not need to be requested again.

## Manage remote work

```sh
elsewhere job-status JOB_ID
elsewhere job-logs JOB_ID
elsewhere job-cancel JOB_ID
elsewhere job-cleanup JOB_ID
```

Use `elsewhere queue` to see waiting/running work and standalone reservations
together. Use `elsewhere dashboard` when the user wants a local control room with
cancel and release controls. Never release another owner's standalone reservation
until the user has decided that work is disposable.

Do not place credentials in commands or Git URLs. Local source transport excludes
common secret patterns, but pattern matching is not a complete secret scanner.

## Product test

“Close the lid” currently means provider execution continues while the originating
device sleeps, then that device resumes status, result recovery, and cleanup from its
local ledger. Do not claim cross-device observation or takeover until a shared or
portable control plane proves that path.
