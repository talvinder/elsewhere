# Developer journey acceptance contract

Accepted 2026-09-07. These requirements describe the target behavior, not a claim
that every step has been verified. This document is authoritative for the agent
workflow; historical release evidence remains in V0.2_ACCEPTANCE.md.

The developer asks their agent to complete and verify a change. They should not
need to mention Elsewhere, select infrastructure, or repeatedly ask for status.

## Required behavior

1. Before a portable build or substantial test run, assess local versus remote
   placement. Local-only work (including Apple device and signing workflows) must
   retain an explicit explanation instead of being exported blindly.
2. Explain the selected location and reason. A capacity denial or sustained local
   wait must prompt a remote assessment, not an unexplained wait. Do not claim
   time savings without measurements.
3. Inspect the exact source, destination, resources, runtime, and estimated cost.
   Reuse an existing matching approval receipt; request a decision only when the
   execution boundary changes. CLI cloud execution requires --execute.
4. Include authorized uncommitted work, exclude secrets, and connect the returned
   result to the exact transported source with a content fingerprint.
5. Retain the job identifier, continue independent work, and check for completion
   without requiring a user reminder. Report meaningful changes rather than
   repeated unchanged status.
6. Recover stdout, exit status, and requested files with checksum verification.
   Failed tests are useful results: inspect them, fix the source, and verify again
   within the approved boundary. Never equate successful dispatch with completion.
7. Preserve recovered results before removing compute and transport artifacts.
   Verify cleanup and provide a concise final receipt covering source, outcome,
   duration, requested resources, and cleanup. Label estimates as estimates.

## Verification gates

- An ordinary change-and-verify request triggers placement without naming Elsewhere.
- A real authorized working-tree change is tested remotely and identified in results.
- A failing test returns useful evidence; the corrected change then passes remotely.
- Results remain recoverable after verified compute and transport cleanup.
- A boundary mismatch prevents source upload and compute creation.
- Local capacity protection remains enforced, including concurrent callers.
- Installation uses a verified candidate independent of an actively edited checkout.

The stronger close-the-lid claim requires separate proof: after disconnecting the
originating device, another device can observe the job, recover results, and verify
cleanup. A background process or durable file on the originating device alone does
not pass this gate. Do not advertise this gate as complete before physical proof.

## Integration ownership

The plugin skill is the canonical Codex workflow. CLI skills must follow the same
placement, trust, result, and cleanup sequence. Compatibility entry points must
delegate to that behavior rather than introduce competing defaults. Admission and
queue responses must make the next action explicit for callers that use the CLI.
