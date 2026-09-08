#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_agx_causal_common.sh"

CONFIG="configs/corrected/hardware_optional_retry_14.json"

if [[ "${RUN_OPTIONAL_HARDWARE_RETRY:-}" != "YES" ]]; then
  echo "Optional retry is disabled by default." >&2
  echo "Read docs/experiment/OPTIONAL_HARDWARE_RETRY.md, then set RUN_OPTIONAL_HARDWARE_RETRY=YES." >&2
  exit 2
fi

require_file "${CONFIG}"
require_file configs/local/agx_board_bindings.json
"${PYTHON_BIN}" scripts/verify_current_data.py

# The config loader verifies that the checkpoint's 82 successful jobs and
# 14-job allowlist exactly partition the original 96-job schedule.
run_causal_with_tracker "${CONFIG}"
