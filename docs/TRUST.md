# Trust and Codex

Provider authentication answers “can this machine talk to Fly or Azure?” It does
not answer “may this agent export these files to that account?” Elsewhere keeps
those questions separate.

## The contract

`elsewhere trust-approve` records a mode-`0600` policy in the user's global
Elsewhere configuration. The policy includes:

- approved Fly apps and Azure subscriptions/resource groups;
- approved compute regions and the source artifact-store account;
- absolute source roots;
- whether private local source and uncommitted or unversioned files may leave;
- maximum CPU, memory, runtime, and estimated per-job cost;
- approval and expiry timestamps.

The receipt is a fingerprint of the complete policy. Changing an account, region,
root, permission, limit, or expiry changes the receipt. Plans reveal mismatches;
execution rejects them.

## Why the Codex plugin matters

An arbitrary shell command hides destination and data boundaries inside text. The
Elsewhere plugin instead gives Codex typed tools with separate read-only planning
and mutating dispatch actions. `elsewhere_dispatch` requires the exact current
receipt and enforces the policy itself before any source is packaged.

This does not weaken or override the generic command sandbox. A raw
`elsewhere dispatch --execute` command may still receive a separate sandbox review.
The plugin is the durable integration boundary: installation establishes the tool,
and the Elsewhere receipt establishes what that tool may export and where.

## Install from this repository

```sh
codex plugin marketplace add .agents
codex plugin add elsewhere@personal
```

Start a new Codex task after installation so the MCP tools are discovered.

For automatic placement assessment, merge the managed block from
`integrations/AGENTS.fragment.md` into the agent's instructions, replacing any older
`agent-capacity` block. Install the CLI skills from `integrations/skill` together
with the plugin skill so they use the same placement-first workflow. Existing
execution boundaries still apply; automatic assessment does not authorize uploads.

After dispatch, `elsewhere_job_wait` accepts a job ID, an optional previous cursor,
and a timeout of 0–30 seconds. It reports meaningful state changes without starting
or deleting compute. Recover verified results and perform authorized cleanup before
reporting completion. See [the acceptance contract](DEVELOPER_JOURNEY.md).

## Review and revoke

```sh
elsewhere trust-status
elsewhere trust-revoke
```

Revocation immediately invalidates the receipt for future dispatch. It does not
terminate already-running provider jobs; use the queue or job controls for those.
