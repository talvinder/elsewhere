---
name: agent-capacity
description: Use Elsewhere to keep work moving beyond the current device. Coordinate shared local capacity and route workloads across local execution, Fly, Azure, and future cloud providers.
---

# Elsewhere

Use the shared capacity command before creating concurrency. A read-only memory
check is insufficient because two agents can check simultaneously and both launch.
Capacity leases make the decision atomic across Codex and Claude.

The command is:

```sh
~/.local/bin/elsewhere
```

## Classify the work

- `parallel-agent`: each additional subagent or parallel agent task
- `browser`: each browser-automation session
- `build`: bundlers, compilers, production builds, or other high-memory jobs
- `test`: test runners that create workers
- `light`: other deliberately parallel background work

Do not acquire capacity for ordinary file reads, searches, or single lightweight
shell commands.

## Reserve before launching

Acquire one lease for the entire requested batch:

```sh
elsewhere acquire \
  --workload parallel-agent --count 2 --owner "codex:current-task"
```

Parse the JSON response. Launch only when `allowed` is `true`. Retain `token` and
release it when all reserved work completes or fails:

```sh
elsewhere release TOKEN
```

If denied, do not launch the requested work. If `recommended_count` is positive,
acquire that smaller count. Otherwise run sequentially or wait until existing work
finishes. Never bypass the gate by splitting one request into simultaneous leases.

Do not hold a lease while waiting for user input. Renew work that legitimately runs
past its lease:

```sh
elsewhere renew TOKEN --ttl 2700
```

## Placement comes before local waiting

For portable builds and substantial tests, use the dry `route` decision below
before choosing local `run`, even when the user has not mentioned Elsewhere.
A capacity denial or queued job requires a remote assessment before settling for
a wait. Explain host-specific work that must remain local. Never duplicate a
queued job: verify cancellation of your own superseded job before dispatching.

Follow remote jobs without a user reminder: recover and inspect verified results,
fix failures within scope, preserve returned files, and verify authorized cleanup.
Report source fingerprint, outcome, duration, resources, estimated cost, and cleanup.
Reuse an existing matching execution approval; never silently broaden its boundary.

## Wrap a single heavy command

Prefer `run` when one command owns the whole workload. It releases automatically:

```sh
elsewhere run --workload build \
  --owner "codex:project-build" -- npm run build
```

If admission is temporarily denied, `run` persists the command as a local job and
starts it when capacity returns. Keep the returned job ID and inspect it with
`job-status` or `job-logs`. Pass `--no-queue` only when immediate denial is required.

Use `elsewhere queue` to show queued jobs and active reservations in one view.
Reservations without a tracked job are labeled as standalone so the user can decide
whether to retain or release them. `elsewhere dashboard` exposes the same state and
safe controls on loopback.

## Inspect capacity

```sh
elsewhere status --human
elsewhere recommend --workload test --max-count 8
```

The memory guard remains the emergency backstop. This skill is the normal control
path that prevents reaching the guard.

## Route work through one decision point

Inspect the provider contract and current preference:

```sh
elsewhere providers
```

Prefer `route` when the caller does not care where work runs. It evaluates local
admission first and returns a dry, explainable decision:

```sh
elsewhere route \
  --workload build \
  --image node:22-bookworm \
  --source-path . \
  --command "npm ci && npm run build"
```

Review the placement reason and, for remote work, `shell_preview`, source selection,
CPU/RAM, cleanup behavior, and provider. Repeat with `--execute` only after review.
The plan must also pass the active `trust-status` receipt, which binds the provider
account, regions, source roots, source-export permission, and job limits.
Use `--execution remote` when the user deliberately wants cloud execution. Use
`dispatch` only when another system has already made the placement decision.

Use an image that contains the repository when `--git-url` is omitted. A private
repository needs provider-native credentials or an image with the source already
inside it. Never place tokens in `--command` or `--git-url`.

Manage submitted jobs with:

```sh
elsewhere job-status JOB_ID
elsewhere job-logs JOB_ID
elsewhere job-cancel JOB_ID
elsewhere job-cleanup JOB_ID
```

Add providers behind the same contract: OCI image, command, CPU, memory, optional
Git source, then dispatch/status/logs/cancel lifecycle operations. Do not leak
provider-specific concepts into callers.
