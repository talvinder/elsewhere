# 0.3.0a1 candidate

This minor-version milestone combines persistent workspace execution and offline
destination cost comparison. It retains alpha status. The package, CLI and Codex
plugin share the same version.

## Publication status

The version bump is an implementation milestone, not a public-release certificate.
Do not publish its tag or release until the existing `--release` gate passes.
The September 10 evidence audit returned 17/25 on `--maturity`:

- The public evidence file contains one maintainer run on macOS, rather than the
  required 25 runs from three participants across macOS and Linux.
- It contains one compute provider and no qualifying intentional failures.
- Its older run lacks current runtime and capture provenance.
- There is no captured independent-user install-to-cleanup journey.
- There is no complete release-matched Fly/Tigris runtime proof.

These are gaps in qualifying evidence, not a claim that no other runs occurred.
The separate [Sprite validation](WORKSPACE_VALIDATION.md) remains valid for its
recorded candidate and must not be relabeled as a 0.3 run.

The narrower public-release gate requires the independent-user journey and
release-matched lifecycle evidence. Beta consideration additionally requires the
broader operational maturity evidence. Use the existing
[capture workflow](../evidence/README.md) to collect real records; do not replace
them with manually asserted completion or synthetic participant identities.
