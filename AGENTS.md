# Agent instructions

Explain user impact before implementation detail. Preserve provider neutrality.

## Public vs private content (hard rule)

This is a **public** repository. Never commit business, strategy, go-to-market,
monetization, pricing, competitive, positioning, partner-integration, or candid
internal-assessment content here. That lives only in the **private** companion repo
`talvinder/elsewhere-internal`.

If unsure whether something is public-appropriate, treat it as private and ask. Before
committing any `.md` that reads like strategy rather than product/engineering docs,
stop and confirm the target repo. Any file may force itself private with the guard
marker formed by `ELSEWHERE:` immediately followed by `INTERNAL`.

Enforcement (install once with `sh scripts/install-hooks.sh`): pre-commit and pre-push
hooks plus a `guard-internal-content` CI check block internal filenames and the marker.

Before changing dispatch behavior:

1. Run `python3 tests/test_cli.py`.
2. Keep cloud execution behind explicit `--execute`.
3. Never commit account IDs, access tokens, SAS URLs, or personal resource names.
4. Add tests for provider planning, source exclusions, redaction, and lifecycle cleanup.
5. Run the full test file and Python compilation before shipping.
