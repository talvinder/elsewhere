#!/bin/sh
# One-time setup: route git hooks at .githooks and make the guard executable.
set -eu
root="$(git rev-parse --show-toplevel)"
cd "$root"
chmod +x .githooks/pre-commit .githooks/pre-push scripts/check-no-internal.sh scripts/install-hooks.sh
git config core.hooksPath .githooks
echo "Guard installed: commits and pushes now block internal content."
echo "CI enforces the same check (guard-internal-content) as a backstop."
