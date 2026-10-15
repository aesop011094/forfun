#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-2}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-2}"
if [[ ! -x .venv/bin/python || ! -f web/dist/index.html ]]; then
  bash scripts/setup.sh
fi
exec .venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
