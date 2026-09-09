# Changelog

All notable changes to this project are documented here.

## [Unreleased]

- Stop treating retained macOS swap as current memory pressure: idle machines
  may run one declared build or test when it fits, while live paging, memory
  stalls, unknown swap at low headroom, and the protected OS floor still block.
- Automatically consume the installed macOS host sampler so sandboxed clients
  receive swap and paging truth without a private environment-variable override.
- Report that sampler as available in `elsewhere doctor`, matching the live
  capacity decision instead of showing a contradictory installation warning.

- Add a Herdr workload pane with explicit execution, project-scoped job receipts,
  and cleanup gated on verified result recovery.

- Assess remote placement before portable verification and when local capacity
  denies work; expose queued placement opportunities with stable event identities.
- Follow job changes through a bounded MCP wait and return completion receipts
  with transported-source fingerprints, results, estimates, and cleanup status.
- Try approved fallback regions when Fly rejects new resources in a retired region.

- Add guided `elsewhere init` and read-only `elsewhere doctor` commands so a new
  user can configure Fly/Tigris or Azure and see the exact remaining setup step.
- Add Fly-native Tigris source and result transport with endpoint pinning,
  credential-safe presigned URLs, authenticated downloads, and verified cleanup.
- Keep explicit project trust denial and revocation from inheriting unrelated global
  approval while preserving compatibility for older project configurations.
- Add Linux and macOS CI coverage for Python 3.11-3.13, public-content guardrails,
  and an executable release gate for current multi-user lifecycle evidence.
- Sense local capacity natively on Linux via `/proc/meminfo` and the `/proc/pressure/memory`
  PSI signal, so the run-here-or-elsewhere decision works without a sampler daemon.
- Report local sensing as explicitly unavailable on unsupported platforms instead of
  silently claiming the machine has zero RAM; remote execution stays available.
- Retry the remote result upload on transient network or 5xx/429 failures so a momentary
  blip no longer discards a completed job's result.
- Match Fly Machines strictly by dispatched Machine ID and read the ID from structured
  JSON first, so a stale same-named machine from a prior run can no longer be mistaken
  for the current job.

## [0.2.0a1] - 2026-07-18

- Prevent quiet retained swap from starving builds, return actionable privacy-safe
  denial details, and add `cleanup --stale` for orphaned reservations and dead local jobs.
- Persist normalized remote state and stable provider identity across processes.
- Scope Fly logs to the exact Machine and retain completed Machines until verified cleanup.
- Put Fly and Azure lifecycle behavior behind a shared provider contract.
- Verify compute and Azure Blob absence before reporting cleanup as complete.
- Protect unavailable results during cleanup, with an explicit `--discard-results`
  escape hatch that still verifies all billable resources are gone.
- Use the package version in MCP metadata and keep repository version labels aligned.
- Return a checksum-verified result bundle containing stdout, stderr, the exact exit
  code, and explicitly requested files after remote compute has stopped.
- Validate MCP requests and tool arguments before any planning or execution action.
- Redact signed URLs and credential-shaped values from persisted and displayed state.
- Verify provider compute, source artifacts, and result artifacts are absent before a
  job reaches `cleaned`.

### Added

- Add a sandbox-safe macOS host sampler for memory pressure, retained swap, and
  swap/page-out activity.
- Add a `service` workload for preview servers and other small persistent work.
- Persist denied local work and resume it automatically when shared capacity returns.
- Add `elsewhere queue` and a loopback control room for waiting/running work,
  recent history, standalone reservations, cancellation, and deliberate release.
- Add durable trust receipts covering provider accounts, regions, artifact storage,
  source roots, private/uncommitted source, resources, runtime, and estimated cost.
- Ship a typed Codex plugin for trust inspection, planning, dispatch, queue visibility,
  and job control.

### Changed

- Replace the below-50%-means-one-slot cliff with adaptive memory budgets, graded
  pressure bands, swap-activity brakes, and lease-age accounting.
- Keep managed local-job leases alive for the lifetime of their process while
  preserving expiry for standalone reservations.
- Rename the product to Elsewhere around the promise that work can continue beyond
  the device where it started.
- Add `elsewhere` as the primary command while retaining `agent-capacity` as a
  compatibility alias.
- Reframe the README around “Your work doesn't need your laptop” and “Close the lid.”
- Document the public product promise, CTFL acceptance test, and copy guardrails.

## [0.1.0] - 2026-07-11

### Added

- Coordinate local memory reservations across Codex, Claude, browser automation, builds, and tests.
- Route OCI workloads through a shared provider contract, with Fly and Azure adapters.
- Retry Fly workloads across regions and fall back between configured providers.
- Package local source safely, excluding common secrets and heavyweight generated folders.
- Transport source through short-lived Azure Blob URLs independently of the compute provider.
- Inspect, log, cancel, and clean up dispatched workloads.
- Verify Azure Blob to Fly source transport with regional retry, URL redaction,
  secret exclusion, and zero-resource cleanup.
