---
name: elsewhere
description: Keep work moving beyond the current device. Use before parallel or memory-heavy work, when choosing local or remote execution, or when managing a workload sent to Fly, Azure, or another provider.
---

# Elsewhere

Elsewhere decides whether work can run safely here or should continue somewhere
else. The user should not need to choose infrastructure unless they want to.

## Route work

Prefer a dry placement decision first:

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
