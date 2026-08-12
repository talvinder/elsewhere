# Releasing Elsewhere

The release is complete only when one version is tested from source, installed from
the built wheel, merged, green on `main`, tagged, and published with no retained
acceptance resources.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
PYTHON=.venv/bin/python sh scripts/quality.sh
PYTHON=.venv/bin/python sh scripts/coverage.sh
.venv/bin/python -m pip install build
.venv/bin/python -m build
```

Install the version-matched wheel into a new virtual environment outside the checkout
(`dist/elsewhere_run-$(cat VERSION)-py3-none-any.whl`) and verify
`elsewhere --help`, `elsewhere mcp-server --help`, and the package version. Scan the
staged diff for credentials, signed URLs, account IDs, and personal cloud resource
names before pushing.

Before making the repository public, run `python scripts/scan_public_history.py`.
Pass the ignored project configuration with `--provider-config .elsewhere.json` to
check the real Fly, Tigris, and Azure destination identifiers without printing them.
Pass the private credential file with `--credentials-env` to check access keys and
secrets the same way. The optional private `--known-values-file` format is
`label=value`, one per line.
This proves locally reachable refs only. If the hosting repository has prior pull
requests containing internal material, publish from a fresh repository rather than
assuming a force-push or branch deletion removes retained pull-request objects.

For an alpha release, merge the reviewed pull request and wait for all Python 3.11,
3.12, and 3.13 jobs on `main`. Create the annotated tag from that exact green commit,
then publish the GitHub release. If post-merge CI fails, do not move or publish the
tag; repair through another pull request and repeat the gate.
