#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "${REPO_ROOT}"
"${PYTHON_BIN}" -m study.campaign \
  --repo-root "${REPO_ROOT}" \
  --config "${REPO_ROOT}/configs/agx_pilot.json" \
  "$@"
"${PYTHON_BIN}" -m study.campaign \
  --repo-root "${REPO_ROOT}" \
  --config "${REPO_ROOT}/configs/agx_pilot_rate_confirmation.json" \
  "$@"
"${PYTHON_BIN}" -m study.campaign \
  --repo-root "${REPO_ROOT}" \
  --config "${REPO_ROOT}/configs/agx_pilot_policy_sweep.json" \
  "$@"
"${PYTHON_BIN}" -m study.campaign \
  --repo-root "${REPO_ROOT}" \
  --config "${REPO_ROOT}/configs/agx_pilot_zero_diagnostic.json" \
  "$@"
exec "${PYTHON_BIN}" "${REPO_ROOT}/scripts/analyze_corrected_pilot.py"
