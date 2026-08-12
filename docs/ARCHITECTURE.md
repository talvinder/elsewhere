# Architecture

Elsewhere separates six concerns that agent tools usually collapse together.

## 1. Local admission control

The CLI combines macOS memory pressure with a local 10-second sample of swap usage
and page-out activity, then maintains short-lived leases under
`/private/tmp/agent-capacity-UID`. A user LaunchAgent publishes that sample so
sandboxed callers see the same host truth without privileged shell access. File
locking makes reservations atomic across Codex, Claude, and terminal sessions.

Swap is never added to available RAM. Quiet retained swap adds a safety margin;
active swap growth can stop bursty admission. Leases count fully during launch and
decay in admission impact after the process has had time to appear in memory
pressure. Work is admitted by remaining memory budget rather than one global slot.

The policy exposes four explainable bands: healthy, guarded, constrained, and
critical. `service` and `light` work can continue deeper into constrained conditions;
agent fan-out, browsers, builds, and tests require more margin.

Managed local jobs renew their lease until the child process exits, then release it.
Standalone reservations retain finite TTL semantics so abandoned agent sessions
cannot claim capacity forever.

## 2. Workload contract

Callers submit a provider-neutral specification:

- workload class
- OCI image
- command
- CPU and memory
- optional Git reference or local source bundle
- requested result paths
- lifecycle actions: dispatch, status, logs, results, cancel, cleanup

Provider-specific flags remain inside adapters.

## 3. Compute adapters

Fly Machines and Azure Container Instances implement the first adapters. A retryable
submission can move to another region or configured provider only after the first
destination proves it created no compute. Ambiguous submissions remain tracked
instead of risking a duplicate billable job.

## 4. Artifact stores

Artifact transport is separate from compute. The Fly-first path packages local files,
uploads them to Tigris through its S3-compatible API, and issues a short-lived
read-only source URL. Azure Blob remains available for Azure-first configurations.
Remote jobs upload checksum-covered results through a short-lived write-only URL; a
later Elsewhere process verifies and retains those results before cleanup.

The shared artifact boundary can also support S3, GCS, R2, or another provider-neutral
object API without changing compute adapters.

## 5. Trust contract

An approval receipt snapshots the actual destination accounts and the maximum
source, region, resource, runtime, and estimated-cost boundary. Planning reports
policy drift without mutating anything. Execution fails closed before packaging,
launch, or fallback when the receipt does not match.

## 6. Local control plane

The queue joins persistent jobs with the shared lease pool. This makes standalone
reservations visible instead of pretending every capacity claim belongs to a known
job. The loopback control room and typed Codex MCP tools read the same state and use
the same cancel, cleanup, and release operations as the CLI.

This control plane is local to the originating device. The job ledger, provider
identity, downloaded result cache, and cleanup state are stored under that device's
private Elsewhere runtime directory. Provider execution does not need the device to
remain awake, but status refresh, result recovery, and cleanup resume there. A shared
or portable control plane for secure cross-device observation and takeover is outside
the current alpha boundary.

## Execution flow

```text
Agent requests work through typed tool or CLI
        |
        v
Local capacity safe? ---- yes ----> acquire lease and run locally
        |
        no
        v
Trust receipt valid? ---- no -----> explain exact boundary mismatch
        |
        yes
        v
Package source -> short-lived artifact -> choose provider -> dispatch OCI job
                                                      |
                                                      v
                                  status / results / cancel / verified cleanup
```

The router makes the decision and can use user-owned infrastructure without changing
the client contract.

## Product boundary

Elsewhere owns the decision and experience layer: whether work fits locally,
where remote work should go, how inputs and results move, what it may cost, and when
resources expire. It does not implement a new isolation runtime.

Execution providers implement a common lifecycle: plan, submit, status, logs,
result delivery, cancel, and cleanup. The first release includes local execution, Fly Machines, and
Azure Container Instances. OpenSandbox is the preferred next adapter because its
Apache-licensed API already covers sandbox lifecycle, resource limits, files,
commands, and multiple runtime backends. OpenShell can be used later for strongly
policy-controlled coding-agent sessions.

## Stable workload contract

Callers describe intent with a workload class, command, CPU, memory, source or Git
input, and optional provider preference. Provider-specific machine identifiers and
storage URLs never enter that caller contract.

`route` evaluates local admission first. Local execution acquires an atomic lease.
Remote execution creates the same job record and chooses an enabled provider using
configured preference and fallback order.

## Artifact boundary

Local source is packaged with a manifest, common secret files and generated folders
are excluded, and a short-lived read-only URL is supplied to the runner. URLs are
redacted before job state is saved. Artifact stores are independent from compute so
future S3, GCS, OSS, or other storage adapters do not alter workload routing.
