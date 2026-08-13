<h1 align="center">Elsewhere</h1>

<p align="center"><strong>Start work here. Let it run anywhere. Close the lid.</strong></p>

<p align="center">A provider-neutral workload router for builds, tests, agents, and data jobs.</p>

<p align="center">
  <a href="https://github.com/talvinder/elsewhere/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/talvinder/elsewhere/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/talvinder/elsewhere/actions/workflows/guard-internal-content.yml"><img alt="Public content guard" src="https://github.com/talvinder/elsewhere/actions/workflows/guard-internal-content.yml/badge.svg"></a>
  <a href="https://github.com/talvinder/elsewhere/blob/main/pyproject.toml"><img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&amp;logoColor=white"></a>
  <a href="https://github.com/talvinder/elsewhere/blob/main/LICENSE"><img alt="Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-0B7285.svg"></a>
  <a href="#project-status"><img alt="Public alpha" src="https://img.shields.io/badge/Status-Public%20alpha-D97706.svg"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="docs/SECURITY.md">Security</a> ·
  <a href="docs/ARCHITECTURE.md">Architecture</a> ·
  <a href="docs/PROVIDER_CONTRACT.md">Provider contract</a> ·
  <a href="docs/ROADMAP.md">Roadmap</a>
</p>

> [!IMPORTANT]
> Elsewhere v0.2 is a public alpha. The local-to-remote lifecycle is working and
> tested across macOS and Linux, but the control plane remains on the originating
> device. See [Project status](#project-status) for the exact boundary.

## Your laptop is a starting point, not a limit

Start a build, agent task, spreadsheet transformation, or data job from the machine
in front of you. Elsewhere checks whether the work fits there. If it does not, it
moves the inputs to compute you already trust, keeps the placement decision visible,
and returns a verified result.

Remote work continues without the originating device staying awake. Reopen that
device to inspect status, recover the result, and verify cleanup.

```sh
elsewhere route \
  --workload build \
  --image node:22-bookworm \
  --source-path . \
  --command "npm ci && npm run build" \
  --result-path dist \
  --execute
```

```text
Local capacity is tight.
Moving this run to Fly Singapore.

Job started · 4 GB · source URL expires in 60 minutes
You can close this laptop.
```

## How it works

| Step | Elsewhere does | You keep control of |
| --- | --- | --- |
| **1. Inspect** | Reads live local capacity and existing reservations. | The workload and its limits. |
| **2. Place** | Explains whether the job should run locally or remotely. | Provider, region, budget, and source boundary. |
| **3. Run** | Executes only after explicit `--execute` approval. | The ability to cancel and inspect logs. |
| **4. Return** | Verifies result checksums and provider cleanup. | The recovered files and durable local receipt. |

Elsewhere gives Codex, Claude, scripts, and eventually everyday applications one
consistent way to submit work. The execution layer can change without changing the
caller contract.

Trust is explicit rather than inferred from cloud credentials. A durable local
approval receipt binds provider accounts, regions, source roots, private and
uncommitted-file permission, CPU, memory, runtime, and estimated cost. Execution is
denied when the request or configured destination drifts from that receipt.

The decision stays visible. So do cost, location, privacy boundaries, and cleanup.
No mystery cloud. No surprise machine left running for the weekend.

## Quick start

Elsewhere requires Python 3.11 or newer. With [`uv`](https://docs.astral.sh/uv/)
installed, inspect your machine and run one harmless local command under Elsewhere's
placement decision:

```sh
uv tool install git+https://github.com/talvinder/elsewhere.git
elsewhere status --human
elsewhere route \
  --workload light \
  --execution local \
  --command "printf elsewhere-ok" \
  --execute
```

You should see `elsewhere-ok` returned with the local capacity decision. This path
needs no cloud account, uploads nothing, and creates no billable resource.

When you are ready to let work leave the laptop, continue to
[Approve the trust boundary](#approve-the-trust-boundary) and the provider setup in
[Connect a cloud provider](#connect-a-cloud-provider). Remote routes stay dry
until you explicitly add `--execute`.

## Inspect jobs and capacity

```sh
elsewhere queue
elsewhere dashboard
```

`queue` puts waiting and running jobs beside active reservations, including work
that was reserved outside the queue. The local dashboard adds safe cancel and
release controls, recent history, and the reason each job is waiting. It binds only
to loopback and uses a per-session control token.

## Approve the trust boundary

```sh
elsewhere trust-approve \
  --provider fly \
  --provider azure \
  --source-root ~/Projects \
  --allow-private \
  --allow-uncommitted \
  --max-cpu 4 \
  --max-memory-mb 8192 \
  --max-runtime-seconds 3600 \
  --max-estimated-cost-usd 5

elsewhere trust-status
```

The receipt is saved with mode `0600`. Dry plans show whether a request fits the
contract. Remote execution rechecks it before packaging source, before launch, and
before provider or region fallback.

## Codex integration

The repository ships a Codex plugin under `plugins/elsewhere`, registered by the
marketplace in `.agents/plugins/marketplace.json`. Its MCP tools
expose trust inspection, non-billable planning, dispatch, queue visibility, and job
control as typed actions. This creates a narrow approval boundary without granting
an arbitrary shell prefix permission to upload files.

See [Trust and Codex](docs/TRUST.md) for installation and the exact enforcement
boundary.

## The close-the-lid contract

This is the test for every product decision. Once Elsewhere accepts a job, you should
not need the originating machine to remain awake while the provider runs it. Inputs
must travel safely, execution must survive independently, and the result must remain
recoverable when that machine resumes.

Today the job ledger, provider identity, result cache, and cleanup controls remain on
the originating device. You can close its lid during remote execution, then reopen
that same device to inspect status, recover the result, and verify cleanup. Observing
or taking over a job from another device still requires a shared control plane or an
explicit portable handoff; Elsewhere does not claim that path yet.

Today the CLI proves that path for builds, tests, and agent workloads. The same
contract can later carry spreadsheet transformations, document processing, media
jobs, simulations, and other work that should not monopolize someone's device.

## How placement works

Every `route` starts as a dry decision. Nothing billable happens until `--execute`
is supplied.

```sh
elsewhere route \
  --workload test \
  --image node:22-bookworm \
  --source-path . \
  --command "npm ci && npm test"
```

Elsewhere considers current memory pressure, existing reservations, workload size,
swap activity, provider preference, and available regions. Retained swap is a warning,
not extra RAM and not an automatic stop. Rapid page-outs pause new bursts; quiet work
can continue when the machine has real headroom. The output explains where the job
would run and why. Add `--execution local` or `--execution remote` when you want to decide.

```sh
elsewhere route \
  --workload test \
  --execution remote \
  --provider fly \
  --image node:22-bookworm \
  --source-path . \
  --command "npm ci && npm test" \
  --execute
```

## Connect a cloud provider

The two-minute local path above works without a provider. To make remote placement
possible, optionally install the macOS sampler and connect infrastructure you
already trust:

```sh
elsewhere sampler-install
```

From a cloned checkout, `python3 -m pip install .` works too.

On macOS, `sampler-install` adds a local LaunchAgent that records memory pressure,
swap retained, and swap/page-out activity every 10 seconds. This lets sandboxed
agents use the same host signal without broader system access. It does not use the
network. Check it with `elsewhere sampler-status`; remove it with
`elsewhere sampler-remove`.

On Linux, no sampler is needed: Elsewhere reads `/proc/meminfo` and the
`/proc/pressure/memory` (PSI) stall signal directly on every call. On platforms where
local sensing is unavailable, Elsewhere says so plainly and remote execution still works.

For the self-contained Fly path, create a dedicated empty Fly app and a Tigris bucket.
`fly storage create` prints the bucket name and standard AWS credentials; export those
credentials in your shell, but never put them in `.elsewhere.json` or a command.

```sh
fly apps create YOUR_RUNNER_APP
fly storage create

elsewhere init \
  --provider fly \
  --fly-app YOUR_RUNNER_APP \
  --tigris-bucket YOUR_TIGRIS_BUCKET

elsewhere doctor --source-path .
```

`init` writes a mode-`0600`, ignored local configuration and prints the exact next
steps for trust approval and a non-billable dry plan. It never creates compute,
uploads source, or approves export on your behalf. Review the trust command before
running it; add `--allow-uncommitted` only when that boundary is intentional.

For Azure compute and Blob transport instead, start from
`examples/config.azure.example.json` or run `elsewhere init --provider azure`.

Provider tools are optional until you use them:

- Fly compute uses `flyctl`; Fly-native artifact transport uses Tigris through its
  standard S3 API and shell-provided AWS credentials.
- Azure compute and Blob transport use `az`.

The old `agent-capacity` command and `.agent-capacity.json` configuration remain
supported during the rename. New installations should use `elsewhere` and
`.elsewhere.json`.

## Coordinate agent workloads

Elsewhere includes a shared-capacity protocol for tools that create parallel workers.
Codex and Claude reserve capacity before fan-out, then release it when work ends.
That prevents both agents from independently deciding the laptop has room for one
more process.

Use `service` for preview servers and other small persistent processes, `light` for
short low-memory commands, and the named `parallel-agent`, `browser`, `build`, or
`test` classes for burstier work. Reservations are charged fully while a process is
starting, then progressively less once the live memory signal already reflects it.
This prevents both launch races and permanent double-counting.

Commands started through `elsewhere run` renew their reservation while the managed
process remains alive, so preview servers stay visible instead of silently outliving
their lease. Explicit `acquire` reservations still expire unless their owner renews
them.

If an owner or managed local worker exits without releasing its reservation, use
`elsewhere cleanup --stale`. Elsewhere re-samples capacity after cleanup and reports
only workload category, age, and declared memory; it hides arguments and environment
values and never terminates untracked processes.

For a single heavy local command:

```sh
elsewhere run --workload build --owner "codex:build" -- npm run build
```

If the laptop cannot admit that command yet, Elsewhere keeps it as a local job
instead of rejecting it. The command starts automatically when the shared lease
pool has room. The response includes a job ID for status, logs, results, cancellation,
and cleanup:

```sh
elsewhere job-status JOB_ID
elsewhere job-logs JOB_ID
elsewhere job-results JOB_ID
elsewhere job-cancel JOB_ID
elsewhere job-cleanup JOB_ID
```

Cleanup normally protects an unavailable result rather than deleting the only
remaining compute evidence. If the result cannot be recovered and you deliberately
want to abandon it, use `elsewhere job-cleanup JOB_ID --discard-results`; Elsewhere
then records the discard and still verifies every remote resource is gone.

Use `--no-queue` only when the caller genuinely wants an immediate capacity error.

For explicit reservation:

```sh
elsewhere acquire \
  --workload parallel-agent \
  --count 2 \
  --owner "claude:feature-work"

elsewhere release TOKEN
```

## Provider model

The open-source router does not force a destination. Fly and Azure are the first
adapters. OpenSandbox is the preferred sandbox-runtime integration. OpenShell can
provide stronger policy controls for full coding-agent sessions. GCP, Kubernetes,
and other providers belong behind the same workload contract.

Elsewhere owns:

- local or remote placement
- capacity and budget policy
- input and result movement
- provider and region selection
- expiry, cancellation, and cleanup
- one consistent experience for the caller

Execution providers own the machinery underneath. Elsewhere wraps them instead of
rebuilding them.

## Security model

Moving work means crossing a data boundary. Elsewhere treats that as a product fact,
not a footnote.

- Planning never uploads source.
- Remote execution requires `--execute`.
- `.env*`, private keys, Git history, dependencies, and generated output are excluded.
- Source bundles contain a file-hash manifest.
- Transport URLs are read-only, short-lived, and redacted from saved job state.
- Compute and uploaded artifacts have explicit cleanup paths.
- Result paths must be relative, cannot traverse outside the workspace, and are
  checksum-verified before Elsewhere reports them as collected.

Remote result delivery expects a POSIX shell plus `timeout`, `tar`, `sha256sum`, and
`curl` in the workload image. The published acceptance path uses
`curlimages/curl:8.10.1`; application images can install the same small toolset.

Pattern-based exclusion cannot identify every secret embedded in an ordinary source
file. Review sensitive workloads before sending them anywhere.

## Verified live path

The first live run packaged an uncommitted local folder, excluded its `.env`, moved
one manifest-tracked file through a short-lived Azure Blob URL, and launched it on an
ephemeral Fly Machine. Mumbai had no capacity, so Elsewhere retried Singapore. The
saved job contained no source credential. Cleanup left zero source blobs and zero
Fly Machines.

That is the beginning, not the destination. The product is done when running work
elsewhere feels as ordinary as running it here.

## Open source

The router, placement policy, provider adapters, runner contract, and agent skill are
open source under Apache 2.0. People can inspect the complete execution boundary and
bring their own infrastructure.

## Project status

Elsewhere v0.2 is an alpha. Local placement, Fly and Azure dispatch, source transport,
regional retry, a same-device durable job lifecycle, verified result return,
idempotent cleanup, and the Codex/Claude integration work today. Cross-device job
observation and takeover require a future shared control plane or portable handoff.
Additional providers and OpenSandbox integration are on the roadmap.

Read the [Architecture](docs/ARCHITECTURE.md), [Security](docs/SECURITY.md),
[Providers](docs/PROVIDERS.md), [Dogfood guide](docs/DOGFOOD.md), and
[Roadmap](docs/ROADMAP.md).

For contributors and release work, see [Contributing](CONTRIBUTING.md), the
[Code of conduct](CODE_OF_CONDUCT.md), [Trust and Codex](docs/TRUST.md), the
[provider contract](docs/PROVIDER_CONTRACT.md), [release process](docs/RELEASING.md),
and the [v0.2 acceptance evidence](docs/V0.2_ACCEPTANCE.md). The v0.2
[design](docs/V0.2_DESIGN.md), [implementation plan](docs/V0.2_IMPLEMENTATION_PLAN.md),
[test plan](docs/V0.2_TEST_PLAN.md), and [public release gate](docs/PUBLIC_RELEASE.md)
preserve the decision and verification trail.

## License

Elsewhere is licensed under [Apache 2.0](LICENSE). You can use it, modify it,
and build commercial products with it. Contributions include an explicit patent
grant under the same license.

---

**Start it. Send it. Close the lid.**
