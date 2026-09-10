# Compare execution estimates

Run `elsewhere compare examples/cost-comparison.json` to compare destinations
before requesting an execution plan. The example uses synthetic rates, not a
provider price quote. Any provider, local host, machine, or persistent workspace
can be represented. No network call, source packaging, or execution occurs.

Supply a version 1 JSON object with `deadline_seconds`, `budget_usd`, and a
nonempty `candidates` array. Each candidate requires every field shown in the
example; missing estimates are rejected rather than treated as free resources.

For each destination, specify runtime, setup and queue delay, expected average
CPU and memory use, fixed hourly and usage-based rates, transfer charges, and
total retained-storage charges for the intended retention period. Use zero for
components that do not apply. Setup is charged at the same declared utilization
as runtime; queue time affects the deadline but is not billed.

Compute cost is billed hours multiplied by the sum of fixed hourly cost,
average CPUs multiplied by CPU-hour cost, and average GB multiplied by GB-hour
cost. Transfer and retained-storage charges are added to get total job cost.

Explicitly supply `ineligible_reasons`: include unmet source-export, account,
region, capacity, continuity, resource, or recovery requirements. An empty list
means the caller asserts these requirements are met. This tool does not discover
or verify eligibility. Budget and deadline violations are added automatically.
Only eligible candidates are ranked, by cost, completion time, then id. If none
qualify, the recommendation is null.

The recommendation is advisory. It never grants authorization or changes routing.
Use the existing route/dispatch flow to recheck live capacity, capability and trust
against the exact workload before explicit execution. Estimates are not billing
guarantees, hard spend caps, live quotes, or measurements of saved human time.

The executable example chooses usage billing at low utilization. Increasing its
average utilization to four CPUs and eight GB selects the fixed-rate candidate.
The tests also cover setup, retained storage, transfer, deadlines, blocked export,
invalid numeric input and missing estimates.
