---
name: elsewhere
description: Route work through the typed Elsewhere trust boundary and manage local or cloud jobs without starving the current device.
---

# Elsewhere for Codex

Use `elsewhere_queue` before starting deliberate parallel or memory-heavy work.
It shows queued jobs and standalone reservations together, including why work is
waiting and the exact owner of each capacity claim.

For remote work:

1. Call `elsewhere_trust_status` and retain the active approval receipt.
2. Call `elsewhere_plan` with the exact provider, source directory, resources,
   runtime, and estimated cost. Planning never uploads source or starts compute.
3. Review the returned trust decision. Do not weaken or split a workload to evade
   a denial.
4. Call `elsewhere_dispatch` only when the plan is allowed, using the unchanged
   receipt. The tool rechecks the contract before source packaging, dispatch, and
   any provider or region fallback.

Use `elsewhere_job_status` for status and retained logs. Use
`elsewhere_job_control` for cancellation, cleanup, or releasing a standalone
reservation. Release another owner's reservation only when the user has decided
that work is disposable.

Never place credentials in commands or Git URLs. A trust receipt approves a
specific boundary; it is not permission to send unrelated files elsewhere.

