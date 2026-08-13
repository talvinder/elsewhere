# Contributing

Elsewhere is early. Keep contributions narrow, provider-neutral, and testable.

## Development

Create an isolated environment and install the project before running checks. This
matches CI and installs both runtime dependencies and the pinned quality tool:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
PYTHON=.venv/bin/python sh scripts/quality.sh
PYTHON=.venv/bin/python sh scripts/coverage.sh
```

After the environment exists, both scripts also discover `.venv/bin/python`
automatically, so `sh scripts/quality.sh` and `sh scripts/coverage.sh` are equivalent.

`scripts/quality.sh` is the contributor and CI source of truth. It checks import and
unused-code hygiene, version consistency, compilation, the full test suite, the
public-content boundary, documentation links, and the current release evidence.
`scripts/coverage.sh` enforces a branch-coverage regression floor. The current 53%
floor is a transparent baseline, not a claim of comprehensive coverage; new behavior
should raise it, especially in the command and lifecycle paths.

Cloud execution must remain opt-in. Tests should generate plans or use isolated fixtures
unless a contributor explicitly chooses a live provider smoke test.

New providers must implement the lifecycle documented in `docs/PROVIDER_CONTRACT.md`. Do not
add provider-specific fields to the public workload contract unless every adapter can
give them a coherent meaning.
