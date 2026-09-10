# Workspace release validation

Verified on 2026-09-10 against Sprites environment `0.0.1-rc48` with synthetic source,
an empty connector inventory, denied outbound network access, and restricted,
expiring validation credentials. Existing application resources were not modified.

## Evidence

The complete lifecycle run used clean commit
`70626e5a6cba6ced5c30e2a4e0c301e88dd96ce0`. Release hardening added naming-scope
enforcement, credential redaction, interrupted-creation recovery, and version metadata.
The final clean release smoke used `bc3a3414450c6ab779b870800bcbd43f65159bcf`, with
Python runtime code SHA-256
`574802b965cd6a10826aee7309a50f551f3462840d1e9c97e098dc7e4fbe2f59`.
Subsequent documentation-only commits preserve this runtime fingerprint.

All runs used transported source fingerprint
`0369c1ddf0f23d108d7dc2243335da5c1a2d23b0df66a818fd1522c062a3301c`.
The task created `review.txt`; recovery checked its exact contents and the result
archive's hashes. A synthetic `.env` file was excluded before transport and from
returned files.

| Journey | Observed result |
| --- | --- |
| Fresh task | Exit 0; real changed file recovered and verified after detachment |
| Approved project | Prior task files remained available; a new task directory completed with exit 0 |
| Isolated tasks | Separate immutable workspace identities and task directories, with the same approved source fingerprint |
| Cancellation | Exact session cancelled; exit 130 and verified partial results recovered |
| Checkpoint | Completed provider stream and exact new checkpoint identity verified |
| Keep and sleep | Retained workspace and released activity hold verified; no forced sleep claim |
| Retention expiry | Preview made no provider calls; execution recovered results and deleted the expired task workspace |
| Project ownership | Task lifetime transferred to project; old task deletion rejected; project survived expiry cleanup |
| Repeated cleanup | Repeated deletion check safely confirmed absence |
| Final release smoke | Exit 0, changed file verified, task deleted, empty validation Sprite inventory |

The test-owned project fixture was deleted separately after proving ordinary project
cleanup preserves it. All validation Sprite resources were confirmed absent. Local
verified result bundles were retained. The first diagnostic run also proved the
exclusive runner marker rejects duplicate invocation with exit 73.

## Automated checks

The final release passes 224 tests, the pre-change CLI regression suite, lint,
Python compilation, public-content guards, documentation checks, and diff checks.
CI covers Python 3.11–3.13 on macOS and Linux, dependency audit, and CodeQL.
Tests include a second provider satisfying the capability contract, approval drift,
unsafe source exclusions, ambiguous operations, expiry, and project ownership.

## Precise limits

This proves detached session continuation and recovery from the originating ledger;
it does not claim a physical lid-close experiment or cross-device control-plane
takeover. Native checkpoint forks remain a separate unsupported capability, as
accepted in WORKSPACE_ISOLATION.md. Retention expiry runs under the developer's
supervisor after reconnect; it is not provider-enforced deletion while that supervisor
is offline. These are the declared product boundaries, not hidden fallback behavior.

This document records source and live evidence. A package installation must separately
match the runtime fingerprint, preserve its previous runtime for rollback, and verify
the activated commands. Merging a pull request alone is not installation evidence.
