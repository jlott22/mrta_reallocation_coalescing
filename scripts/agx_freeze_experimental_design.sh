#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_agx_causal_common.sh"

if [[ "${ACCEPT_REVIEWED_DESIGN:-}" != "YES" ]]; then
  echo "Set ACCEPT_REVIEWED_DESIGN=YES only after reading all native reports." >&2
  exit 2
fi
FREEZE_DIR="${FREEZE_DIR:-study/frozen/native_causal_v1}"
"${PYTHON_BIN}" -m study.causal.freeze \
  --repo-root "${REPO_ROOT}" \
  --base-config study/native_gates/calibration/variance_stage_config.json \
  --gate study/native_gates/environment/native_environment_report.json \
  --gate study/native_gates/preflight/native_preflight_report.json \
  --gate study/native_gates/smoke/causal_smoke_report.json \
  --gate study/native_gates/calibration/rate_calibration_report.json \
  --gate study/native_gates/calibration/timeout_calibration_report.json \
  --gate study/native_gates/calibration/variance_calibration_report.json \
  --output-dir "${FREEZE_DIR}" \
  --accept-reviewed-proposals
