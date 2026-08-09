#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CONFIG_PATH="${1:-${REPO_ROOT}/configs/agx_full.json}"
if [[ $# -gt 0 ]]; then
  shift
fi

cd "${REPO_ROOT}"
exec "${PYTHON_BIN}" -m study.analysis \
  --repo-root "${REPO_ROOT}" \
  --config "${CONFIG_PATH}" \
  "$@"
