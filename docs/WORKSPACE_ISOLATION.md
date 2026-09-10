# Workspace isolation clarification

Accepted 2026-09-10. This clarification extends WORKSPACE_EXECUTION.md without
changing its developer-supervised boundary. It resolves that document's open
question about isolated repository tasks.

Implement fresh workspaces, approved existing project workspaces, and isolated task
workspaces seeded from a verified repository snapshot. Snapshot isolation preserves
the exact approved files and source lineage. Dependencies may need rebuilding.
An optional parent workspace/checkpoint reference records provenance; it does not
claim the filesystem was cloned from that checkpoint.

Native checkpoint fork is a separate optional capability. It preserves only the
environment state guaranteed by that provider. If requested, it must be supported
and verified; otherwise planning fails before mutation. Never silently replace it
with a repository snapshot. Sprites native fork remains unavailable until verified.

The provider-neutral caller contract describes disposable jobs, continuing project
work, fresh task work, or isolated task work. Adapters declare container execution,
persistent files, reconnectable sessions, checkpoints, native forks, network
controls, and supported lifecycle actions. Selection must satisfy required
capabilities rather than use one provider as a universal default.

Use Fly Machines and Azure for compatible disposable OCI jobs, and Sprites for
compatible continuing work. Future providers implement these same guarantees and
declare their differences. Device-bound work remains local under admission policy.

Executable guards must reject native-fork requests on adapters without that
capability, reject incompatible fallback, and distinguish snapshot lineage from
environment checkpoint lineage. This is an accepted contract, not release evidence.
