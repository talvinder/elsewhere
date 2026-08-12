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
CONFIG=$(pwd)/pyproject.toml

"$PYTHON" -m coverage erase
# test_cli.py is an executable end-to-end journey rather than a unittest module.
# Measure it explicitly so the coverage gate represents the same product behavior
# that scripts/quality.sh certifies.
COVERAGE_PROCESS_START="$CONFIG" PYTHONPATH=src \
  "$PYTHON" -m coverage run tests/test_cli.py
COVERAGE_PROCESS_START="$CONFIG" PYTHONPATH=src \
  "$PYTHON" -m coverage run -m unittest discover -s tests -p 'test_*.py'
"$PYTHON" -m coverage combine
"$PYTHON" -m coverage report
"$PYTHON" -m coverage erase
