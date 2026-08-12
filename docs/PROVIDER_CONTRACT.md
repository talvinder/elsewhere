# Compute provider contract

Elsewhere keeps the customer-facing lifecycle provider-neutral. A compute adapter
translates provider commands and responses; it does not decide trust, source scope,
cost limits, durable state, or whether execution is allowed.

## Required interface

Every enabled compute provider implements the `ComputeProvider` protocol in
`src/agent_capacity/providers/base.py`:

- build an execution plan without starting compute
- extract stable provider identity from submission evidence
- build a status command and normalize its response
- return logs scoped to the exact provider job
- build cancellation and cleanup commands
- classify failures as retryable or terminal
- declare the provider's remote result strategy
- report readiness, configured identity, and supported regions

Status returns `ProviderObservation`, containing a provider-neutral state, stable
identity, explicit absence signal, and bounded evidence. A missing resource is not
success. The lifecycle layer preserves the last known outcome unless it has direct
completion evidence.

## Adding a provider

1. Add one module under `src/agent_capacity/providers/`.
2. Implement every method in `ComputeProvider`.
3. Register one instance in `providers/__init__.py`.
4. Map provider states into Elsewhere's lifecycle vocabulary in `models.py`.
5. Add realistic response fixtures for queued, running, terminal, missing, cancel,
   cleanup, and repeated cleanup behavior.
6. Keep account-specific values inside `provider_config`; never put credentials or
   signed artifact URLs into a plan or saved observation.
7. Keep execution behind the existing trust receipt and explicit `--execute` gate.

The registry rejects an enabled provider that does not implement the complete
contract. Trust, artifact transport, result verification, redaction, and durable job
state remain shared responsibilities rather than provider-specific behavior.

## Current adapters

- Fly retains a Machine after exit so one job's logs and completion evidence remain
  inspectable. Elsewhere later destroys it and verifies absence.
- Azure uses one Container Group per Elsewhere job, reads its container exit code,
  deletes the group, and verifies a not-found response.

Both adapters deliver results through the approved artifact store. Cleanup is
blocked until a terminal job's result bundle is collected, except when submission
never created compute or the user cancelled the job. If a result is permanently
unavailable, `--discard-results` records the deliberate loss before verified cleanup.
