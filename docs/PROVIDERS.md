# Provider adapters

Provider adapters are execution engines, not competing product experiences. The
router keeps placement, budgets, transport, expiry, and cleanup stable while each
adapter translates that contract into a provider lifecycle.

## Sandbox runtimes

[OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) is the preferred
next adapter. It already provides an Apache-licensed lifecycle API, command and file
operations, resource limits, MCP/SDK clients, and Docker/Kubernetes backends.

[NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) is a future optional adapter
for complete coding-agent sessions needing declarative filesystem, process,
credential, and network policy. Its current alpha status makes it unsuitable as the
only runtime for the first release.

## Compute contract

Every compute adapter accepts an OCI image, command, CPU, memory, optional source,
requested result paths, and the lifecycle operations dispatch, status, logs, results,
cancel, and cleanup.

## Fly

Fly Machines are retained after exit until Elsewhere has collected results. The
adapter retries configured regions only after Fly proves the earlier submission did
not create a Machine. Configure a dedicated empty Fly app; do not reuse a production
application. The recommended first-run path uses Fly-provisioned Tigris object storage
for source and result transport, so Fly users do not need an Azure account.

## Azure

Azure Container Instances run with `restart-policy Never`. Completed container groups
are deleted after verified results are collected. Keep workloads in a dedicated resource group.

## Adding GCP or another provider

Implement the same lifecycle without changing the public workload format. Suitable
targets include Cloud Run Jobs, Kubernetes Jobs, raw VMs with a runner image, or a
provider-specific batch service.

The adapter must define:

1. readiness checks
2. dispatch command/API request
3. stable job identity
4. status normalization
5. log collection
6. cancellation
7. cleanup and cost-stop semantics
8. retryable versus terminal failures
9. remote result-delivery strategy

## Artifact stores

Artifact storage is a separate extension point. Tigris and Azure Blob implement the
same upload, short-lived source read, short-lived result write, authenticated result
download, and verified deletion operations. Future stores must preserve that contract.
