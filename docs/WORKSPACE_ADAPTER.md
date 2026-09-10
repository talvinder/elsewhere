# Workspace adapter candidate

This is an implementation candidate for WORKSPACE_EXECUTION.md and its accepted
WORKSPACE_ISOLATION.md clarification. It is not an installed or live-certified release.

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
Native-fork requests fail closed in this candidate.

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
Cleanup does not discard unrecovered results, even after cancellation.

Retention deadlines are recorded for the originating supervisor. This candidate does
not provide an always-on expiry service or a provider-enforced deletion deadline.
Keep, sleep, and checkpoint retention therefore require supervisor follow-through;
release certification must establish the acceptable expiry behavior before claiming
bounded unattended retention. No paid resource is created by planning or tests.

## Validation boundary

Automated checks cover capability selection, unsupported forks, trust drift, source
exclusion, identity mismatch, interrupted streams, cancellation, retained workspaces,
and repeated cleanup. A real local subprocess exercises source transport, changed
files, verified recovery, and duplicate prevention.

Live Sprites certification is still required: authenticate in a dedicated test
organization, verify actual connector and checkpoint response shapes, run fresh and
approved-project tasks, run two isolated snapshots, reconnect, recover a change, and
verify task-owned deletion. No live claim follows from mocked API fixtures. Installation
remains a separate explicitly approved action after exact candidate evidence.
