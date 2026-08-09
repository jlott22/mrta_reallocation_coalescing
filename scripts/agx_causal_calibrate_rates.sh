#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_agx_causal_common.sh"

CONFIG="configs/agx_causal_rate_calibration_v1.json"
"${PYTHON_BIN}" -m study.causal.orchestrator --repo-root "${REPO_ROOT}" --config "${CONFIG}"
mkdir -p study/native_gates/calibration

REVIEW_ARGS=()
if [[ -n "${REVIEW_LOW_LOAD:-}" || -n "${REVIEW_MEDIUM_LOAD:-}" || -n "${REVIEW_HIGH_LOAD:-}" ]]; then
  : "${REVIEW_LOW_LOAD:?Set all REVIEW_LOW_LOAD, REVIEW_MEDIUM_LOAD, REVIEW_HIGH_LOAD}"
  : "${REVIEW_MEDIUM_LOAD:?Set all REVIEW_LOW_LOAD, REVIEW_MEDIUM_LOAD, REVIEW_HIGH_LOAD}"
  : "${REVIEW_HIGH_LOAD:?Set all REVIEW_LOW_LOAD, REVIEW_MEDIUM_LOAD, REVIEW_HIGH_LOAD}"
  REVIEW_ARGS+=(--review-rate "low=${REVIEW_LOW_LOAD}")
  REVIEW_ARGS+=(--review-rate "medium=${REVIEW_MEDIUM_LOAD}")
  REVIEW_ARGS+=(--review-rate "high=${REVIEW_HIGH_LOAD}")
fi
if [[ -n "${REVIEW_RATE_JUSTIFICATION:-}" ]]; then
  REVIEW_ARGS+=(--review-rate-justification "${REVIEW_RATE_JUSTIFICATION}")
fi

"${PYTHON_BIN}" -m study.causal.calibration rates \
  --input-root study/output/agx_causal_rate_calibration_v1/causal/completed \
  --config "${CONFIG}" \
  "${REVIEW_ARGS[@]}" \
  --output study/native_gates/calibration/rate_calibration_report.json
"${PYTHON_BIN}" -m study.causal.reports calibration \
  --input study/native_gates/calibration/rate_calibration_report.json \
  --markdown study/native_gates/calibration/NATIVE_RATE_CALIBRATION_REPORT.md

echo "Review the proposal. Re-run with REVIEW_LOW_LOAD, REVIEW_MEDIUM_LOAD, and REVIEW_HIGH_LOAD; set REVIEW_RATE_JUSTIFICATION if overriding it."
