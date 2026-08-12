# Roadmap

## Open core

- OpenSandbox lifecycle, command, file, and resource-limit adapter
- OpenShell adapter for policy-controlled coding-agent sessions
- GCP and generic Kubernetes Job adapters
- GCS and R2 artifact stores behind the existing object-transport contract
- resumable large-result transfer and provider-native artifact stores
- provider-native hard spend caps and live price estimation (the local trust contract
  already gates declared per-job estimates plus CPU, memory, and runtime)
- policy hooks for organizations
- signed runner images and bundle attestation
- dynamic adapter discovery

## Product validation

The first promise to validate is narrow: submit one workload, let the router choose
local or remote execution, and get a result without freezing the laptop. Coding-agent
builds and tests are the first wedge. Large spreadsheets and document/data processing
are later workflows with stricter privacy and application-compatibility requirements.
