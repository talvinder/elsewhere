# Workspace execution

This implements WORKSPACE_EXECUTION.md and its accepted WORKSPACE_ISOLATION.md
clarification. See [live validation](WORKSPACE_VALIDATION.md) for the tested journeys,
candidate identity, and deliberately separate installation state.

## Developer workflow

Existing OCI `route` and `dispatch` requests continue using Fly Machines or Azure.
A continuing-work request uses `route --workspace-spec request.json`, or the typed
`elsewhere_workspace_plan` and `elsewhere_workspace_dispatch` tools. Status, result
recovery, cancellation, and cleanup use the existing job commands and ledger.

A workspace request selects capabilities rather than assuming any cloud can execute
it. The workspace registry is separate from the disposable-compute registry because
workspace identity, session identity, and retention have different lifetimes. The
provider-neutral protocol lives in `src/agent_capacity/workspace_contract.py`.
Sprites is the first implementation. Provider-specific HTTP paths and activity holds
live in its adapter; the shared runner does not carry provider credentials.

Example request (use an approved policy appropriate for the actual command):

```json
{
  "intent": "isolated",
  "derivation": "snapshot",
  "retention": "delete",
  "retention_seconds": 0,
  "network_policy": {"rules": [{"domain": "*", "action": "deny"}]},
  "connectors": [],
  "resources": {"cpu": 8, "memory": "provider-managed", "storage_gb": 100}
}
```

`fresh` and `isolated` create independently named workspaces. `project` requires
`project: {"name": "example-project", "id": "immutable-provider-id"}` and may only
keep or allow idle pause. Every invocation receives a fresh task directory seeded
from the reviewed local repository; project-level installed tools and caches persist.
It does not overwrite a pre-existing repository directory in the project workspace.

Optional `parent` has `name`, immutable `id`, and optional `checkpoint`. The parent
and checkpoint are checked before source transfer. The receipt calls this a repository
snapshot; it does not claim to have copied the parent's filesystem or caches.
Native-fork requests fail closed.

Configure `providers.sprites.enabled` and `providers.sprites.organization` in a
private Elsewhere config. Supply the API token through `SPRITES_TOKEN` or
`SPRITE_TOKEN`; it is never included in the plan or command arguments. The token's
organization is checked before creation. No candidate activation is required to test
from an isolated virtual environment.

```sh
elsewhere route --workload parallel-agent --provider sprites \
  --workspace-spec request.json --source-path /absolute/approved/source \
  --command 'python3 task.py' --max-runtime-seconds 120 \
  --estimated-cost-usd 0.10
```

Review `plan.workspace_boundary` and save that object to a private JSON file. The
existing `trust-approve` command accepts `--provider sprites` and
`--workspace-boundary boundary.json`, along with source/resource/runtime/cost limits.
Approval explicitly accepts provider-managed memory: no fixed OCI memory ceiling is
claimed for Sprites. An old Machines receipt does not authorize this provider.
Repeat the route with `--execute --approval-receipt RECEIPT` only after that review.

Connector approval contains the complete inspected organization inventory projected
as `id`, `provider`, and `access_policy`. Extra connectors or policy changes stop
export. Credentials are never projected. This version reads existing connector
policies; it does not provision connectors or enlarge their grants. An unfamiliar
provider response schema fails closed pending live verification.

## Results and retention

The task records source hashes and lineage, runs with a bounded disconnect lifetime
and expiring activity hold, and writes a checksum-covered result archive. A repeated
runner invocation cannot rerun the command. Result recovery verifies the archive,
job identity, requested paths, and source lineage before accepting completion.
Returned repository files remain local for developer review, including after remote
deletion. Excluded generated directories and sensitive-file patterns remain excluded
from results. File deletion can be determined by comparing the source manifest with
the returned tree; no patch is automatically applied.

A Sprite's cold/warm/running state is not used as task exit evidence. Missing files
or disconnected sessions remain unresolved. Ambiguous mutations are retained in the
ledger and are not automatically retried into another destination.

`job-cleanup` applies the approved action after verified recovery: keep, permit idle
pause, create and identify a checkpoint, or delete and verify absence. Project
workspaces cannot be deleted by task cleanup. Cancellation targets one session.
Cleanup preserves unrecovered results after cancellation unless the developer
explicitly supplies `--discard-results`. A recorded preparation failure before
session submission can delete its task-owned workspace without inventing results.

Retention is supervised by the originating developer. The reviewed boundary names
the expiry action: delete a task-owned workspace after verified recovery, or release
the task's claim on an existing project. The deadline is submission preparation plus
the maximum runtime and retention duration; status polling never extends it.
`workspace-reap` previews overdue entries without provider calls. Add `--execute` to
recover results and apply each entry's approved expiry action. Failures remain in the
ledger for retry; expired credentials or changed authority stop mutation. The command
returns a nonzero status when an entry is blocked. Run it after reconnecting or from
the developer's existing scheduler. No hosted supervisor is introduced. Storage can
remain billable while that supervisor is offline: this is not a provider-enforced
wall-clock deletion guarantee. Runtime and activity holds still expire remotely.

Selecting an exact retained task workspace as a project transfers its lifetime to
the approved project and prevents the originating task from deleting it later.
Ordinary `job-cleanup` also applies expiry when overdue; it never discards results
implicitly. Planning and unit tests create no paid resources.

The boundary includes the source content fingerprint and privilege policy (the
explicit default is `{}`). Source drift before creation or during packaging stops
export. Existing and newly created workspaces must match the approved privileges.
`job-logs` can reconnect to the original session for diagnostics, but session exit
alone never substitutes for a verified result bundle. Ambiguous session submission
can recover a unique session whose command names the exact task runner directory.

## Validation boundary

Automated checks cover capability selection, unsupported forks, trust drift, source
exclusion, identity mismatch, interrupted streams, cancellation, retained workspaces,
and repeated cleanup. A real local subprocess exercises source transport, changed
files, verified recovery, and duplicate prevention.

Live Sprites certification used a token restricted to task names, creation count,
and expiry. It verified connector and checkpoint response shapes, fresh and approved
project tasks, isolated snapshots, reconnection, changed-file recovery, cancellation,
checkpoint retention, idle pause permission, supervised expiry, and verified deletion.
The runner establishes its expiring activity hold and starts the bounded child before
reporting readiness; detaching on session identity alone is insufficient. Creation
reconciliation requires the exact generated name, a recorded absence before submission,
and provider creation time matching that attempt; it never silently submits a session.
Installation remains a separate action after exact candidate evidence.
