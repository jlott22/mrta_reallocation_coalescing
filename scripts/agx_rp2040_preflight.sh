#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_agx_causal_common.sh"

CONFIG="${1:-configs/corrected/hardware_optional_retry_14.json}"
require_file "${CONFIG}"
require_file configs/local/agx_board_bindings.json
mkdir -p study/native_gates/preflight
"${PYTHON_BIN}" -m study.causal.native --repo-root "${REPO_ROOT}" \
  preflight \
  --config "${CONFIG}" \
  --json study/native_gates/preflight/native_preflight_report.json \
  --markdown study/native_gates/preflight/NATIVE_PREFLIGHT_REPORT.md
