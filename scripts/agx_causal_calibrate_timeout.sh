#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_agx_causal_common.sh"

RATE_REPORT="study/native_gates/calibration/rate_calibration_report.json"
require_file "${RATE_REPORT}"
CONFIG="study/native_gates/calibration/timeout_stage_config.json"
"${PYTHON_BIN}" -m study.causal.stage_config timeout \
  --repo-root "${REPO_ROOT}" \
  --rate-report "${RATE_REPORT}" \
  --rate-config configs/agx_causal_rate_calibration_v1.json \
  --output "${CONFIG}"
run_causal_with_tracker "${CONFIG}"

CAMPAIGN_ID="$(${PYTHON_BIN} -c 'import json,sys; print(json.load(open(sys.argv[1]))["campaign"]["campaign_id"])' "${CONFIG}")"
REVIEW_ARGS=()
if [[ -n "${REVIEW_TIMEOUT_S:-}" ]]; then
  REVIEW_ARGS+=(--review-timeout-s "${REVIEW_TIMEOUT_S}")
fi
if [[ -n "${REVIEW_TIMEOUT_JUSTIFICATION:-}" ]]; then
  REVIEW_ARGS+=(--review-timeout-justification "${REVIEW_TIMEOUT_JUSTIFICATION}")
fi
"${PYTHON_BIN}" -m study.causal.calibration timeout \
  --input-root "study/output/${CAMPAIGN_ID}/causal/completed" \
  --config "${CONFIG}" \
  "${REVIEW_ARGS[@]}" \
  --output study/native_gates/calibration/timeout_calibration_report.json
"${PYTHON_BIN}" -m study.causal.reports calibration \
  --input study/native_gates/calibration/timeout_calibration_report.json \
  --markdown study/native_gates/calibration/NATIVE_TIMEOUT_CALIBRATION_REPORT.md

echo "Review the proposal. Re-run with REVIEW_TIMEOUT_S=2|5|10|20; set REVIEW_TIMEOUT_JUSTIFICATION if overriding it."
