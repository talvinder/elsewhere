# Developer-supervised workspace execution

Accepted 2026-09-10. This is the target engineering contract, not a claim of
implemented or verified Sprites support. It extends DEVELOPER_JOURNEY.md. The
developer remains the supervisor and reviews returned changes. Autonomous
supervision and a hosted multi-tenant control plane are outside this phase.

The accepted [isolation clarification](WORKSPACE_ISOLATION.md) resolves the native
fork gate below: verified repository snapshot isolation is the first implementation;
native checkpoint forks remain a distinct optional capability.

## Placement and ownership

Elsewhere first decides what kind of execution the work needs, then where it may
run. A busy laptop does not make every remote destination interchangeable.

| Contract | Intended work | Placement |
| --- | --- | --- |
| Local | Small work, or work needing the developer's device, signing identity, or local application | Existing admission and lease policy |
| Disposable OCI job | Bounded builds/tests in a declared container image | Existing Fly Machines or Azure adapter |
| Persistent project workspace | Continuing agent work in an approved environment | Explicitly approved existing Sprite |
| Fresh task workspace | Continuing work with a new, separately owned environment | New task Sprite |
| Forked task workspace | Isolated parallel repository work derived from a known parent checkpoint | Separate task Sprite with verified parent and checkpoint lineage |

Automatic inputs are observed local capacity, source fingerprint and dirty state,
provider readiness, and capability compatibility. The user or approved policy
controls portability, workspace intent, project selection, parent checkpoint,
parallel isolation, network policy, connector access, resource ceilings, runtime,
estimated cost, and retention. Unknown portability must not export device-bound
work. No fallback may silently change the execution contract or source boundary.

## Runtime contract

Keep the existing compute lifecycle for OCI jobs. Add a workspace capability
contract for create/select, session execution and reconnection, files, checkpoints,
isolated derivation, policy inspection, cancellation, and retention. Adapters
translate supported APIs; shared orchestration owns authorization and the ledger.
Unsupported capabilities fail during planning, before packaging or mutation.

Record workspace identity separately from task/session identity. Workspace warmth,
hibernation, or deletion is never task completion evidence. An existing project
workspace requires an exact approved identity and exclusive mutation ownership;
parallel tasks require separate workspaces. Never restore or delete a shared
project as a side effect of completing or cancelling one task.

Persist submission intent before a remote mutation. Reconcile ambiguous create or
exec responses against exact identity. Do not retry into another workspace or
provider until absence of the original operation is proved. Read retries are
bounded; mutation retries require operation-specific idempotency evidence.

## Authority and lineage

Cloud execution requires explicit --execute and the active matching trust receipt.
An existing Fly Machines grant does not authorize Sprites. Extend the same approval
boundary to organization, exact existing Sprite identity or new-task naming scope,
source root and content fingerprint, parent identity and checkpoint, network and
privilege policy, connector IDs and allowed operations, resource limits, maximum
runtime, estimated cost, and retention duration/action. Missing or changed values
fail closed. Secrets stay outside command arguments, public projections, and Git.

Source exclusions and redaction apply before transport. The result receipt links
the local revision, dirty-source fingerprint, transported manifest, task ID,
workspace ID, session ID, and parent/checkpoint lineage. A repository snapshot copy
must never be labelled a provider-native filesystem checkpoint fork.

Recover stdout, stderr, exit evidence, changed files or patch, and requested
artifacts to the developer's result cache. Verify hashes and safe paths before
review. Disconnecting the client must not lose the ledger or authorize duplicate
execution. Lost sessions without exit evidence remain unresolved, not successful.

## Completion and retention

Keep task outcome, result verification, and workspace retention as separate states.
The approved lifecycle deliberately chooses:

- keep: preserve the workspace within a bounded retention grant;
- sleep: release task-owned activity holds and allow provider-managed idle pause;
  report the observation rather than promise a forced sleep transition;
- checkpoint: verify completion of a snapshot and retain its exact identity;
- delete: preserve verified results first, delete only task-owned resources, then
  prove absence. Repeated cleanup must be safe.

Cancellation targets the exact task/session and its activity holds. It does not
imply that partial results were recovered or that a project workspace was removed.
Services must not restart a completed coding task. Activity holds and heartbeats
must expire within the authorized runtime even if the originating Mac disconnects.

## Verified provider capabilities and open gate

Reviewed official references on 2026-09-10:

- [Sprites API](https://sprites.dev/api/sprites): create, list, get, update, delete;
  bearer authentication and organization-scoped identities.
- [Exec](https://sprites.dev/api/sprites/exec) and
  [files](https://sprites.dev/api/sprites/filesystem): command sessions, attachment,
  cancellation, and filesystem operations.
- [Checkpoints](https://sprites.dev/api/sprites/checkpoints): snapshot creation,
  enumeration, inspection, and restore within a named Sprite. Stream completion
  must be checked; receiving HTTP 200 alone is insufficient.
- [Policy](https://sprites.dev/api/sprites/policies) and
  [connectors](https://docs.sprites.dev/concepts/connectors/): inspect and enforce
  approved access. Connector credentials remain with the provider gateway.
- [Services](https://sprites.dev/api/sprites/services) and
  [tasks](https://docs.sprites.dev/keeping-sprites-running/): distinguish restartable
  processes from expiring activity holds. Tasks use the in-Sprite management socket.
- [Persistence](https://docs.sprites.dev/concepts/lifecycle/): filesystem durability
  does not guarantee process survival after cold wake.

The reviewed create/checkpoint API does not document cross-Sprite checkpoint
forking. The provider's [copy request](https://github.com/superfly/sprites-docs/issues/137)
remains open. Native checkpoint fork therefore remains an unverified capability;
do not invent an endpoint or substitute repository copying without an explicit
contract clarification. Resource and process-persistence claims also require live
verification against the actual environment version, not assumptions from examples.

## Acceptance gates

Regression coverage must include classification, unchanged Fly/Azure execution,
source exclusions, redaction, trust drift, exact project selection, checkpoint
lineage, parallel isolation, result recovery, ambiguous mutations, cancellation,
retention, repeated cleanup, and provider-neutral public receipts.

Before release, prove fresh, existing-project, and isolated-task journeys in an
approved safe integration environment. Recover a real change for developer review,
reconnect after disconnection, and verify the chosen retention action. Mocked tests
alone do not complete this contract. Keep committed, pushed, merged, installed, and
developer-verified status separate. Installation requires candidate provenance,
validation, rollback, and explicit approval.
