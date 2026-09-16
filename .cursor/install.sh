#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for ABCXAUTO.
# Creates a project virtualenv and installs pinned dependencies.
set -euo pipefail

cd "$(dirname "$0")/.."

# `python -m venv` needs ensurepip, which Debian/Ubuntu ships in python3.x-venv.
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y python3-venv >/dev/null
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo "ABCXAUTO install complete: $(.venv/bin/python --version)"
