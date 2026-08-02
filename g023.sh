#!/usr/bin/env bash
# g023 Code launcher (Linux / macOS / WSL)
# Usage from any project folder:
#   /path/to/g023-code/g023.sh
#   or after adding to PATH / alias: g023

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export G023_HOME="${SCRIPT_DIR}"
export G023_PROJECT_ROOT="$(pwd)"

# Prefer python3, fall back to python
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "Error: Python 3 is required but not found." >&2
    exit 1
fi

# Ensure the package is importable
export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:$PYTHONPATH}"

exec "${PYTHON}" -m g023_code "$@"
