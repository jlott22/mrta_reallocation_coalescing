#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_agx_causal_common.sh"

CONFIG="${1:-configs/agx_causal_smoke_v1.json}"
require_file "${CONFIG}"
require_file configs/local/agx_board_bindings.json
ACK_ARGS=()
if [[ "${ACCEPT_RECORDED_POWER_CLOCK_STATE:-}" == "YES" ]]; then
  ACK_ARGS+=(--accept-recorded-power-clock-state)
else
  echo "This first run records a FAIL report for review. Inspect it, then rerun with ACCEPT_RECORDED_POWER_CLOCK_STATE=YES." >&2
fi
mkdir -p study/native_gates/environment
"${PYTHON_BIN}" -m study.causal.environment \
  --repo-root "${REPO_ROOT}" \
  --config "${CONFIG}" \
  --output-dir study/native_gates/environment \
  "${ACK_ARGS[@]}"
