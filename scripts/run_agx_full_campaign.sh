#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "${REPO_ROOT}"
exec "${PYTHON_BIN}" -m study.campaign \
  --repo-root "${REPO_ROOT}" \
  --config "${REPO_ROOT}/configs/agx_full.json" \
  "$@"
