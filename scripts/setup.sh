#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$PWD/data/pip-cache}"
export npm_config_cache="${npm_config_cache:-$PWD/data/npm-cache}"
FIA_VENV_PATH="${FIA_VENV_PATH:-.venv}"
python3 -m venv "$FIA_VENV_PATH"
"$FIA_VENV_PATH/bin/python" -m pip install --disable-pip-version-check -r requirements.txt
(cd web && npm ci && npm run build)
mkdir -p data
