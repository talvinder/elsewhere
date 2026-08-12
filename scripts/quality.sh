#!/bin/sh
set -eu

if [ -z "${PYTHON:-}" ] && [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
else
  PYTHON=${PYTHON:-python3}
fi
PYTHON_BIN=$("$PYTHON" -c 'import pathlib, sys; print(pathlib.Path(sys.executable).parent)')
PATH="$PYTHON_BIN:$PATH"
export PATH

"$PYTHON" -m ruff check src scripts tests
"$PYTHON" scripts/sync_version.py --check
"$PYTHON" -m compileall -q src tests scripts
"$PYTHON" tests/test_cli.py
PYTHONPATH=src "$PYTHON" -m unittest discover -s tests -p 'test_*.py'
sh scripts/check-no-internal.sh --tracked
"$PYTHON" scripts/public_readiness.py
git diff --check
