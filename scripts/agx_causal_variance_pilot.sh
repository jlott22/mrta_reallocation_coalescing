#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_agx_causal_common.sh"

TIMEOUT_CONFIG="study/native_gates/calibration/timeout_stage_config.json"
TIMEOUT_REPORT="study/native_gates/calibration/timeout_calibration_report.json"
require_file "${TIMEOUT_CONFIG}"
require_file "${TIMEOUT_REPORT}"
CONFIG="study/native_gates/calibration/variance_stage_config.json"
"${PYTHON_BIN}" -m study.causal.stage_config variance \
  --repo-root "${REPO_ROOT}" \
  --timeout-config "${TIMEOUT_CONFIG}" \
  --timeout-report "${TIMEOUT_REPORT}" \
  --output "${CONFIG}"
run_causal_with_tracker "${CONFIG}"

CAMPAIGN_ID="$(${PYTHON_BIN} -c 'import json,sys; print(json.load(open(sys.argv[1]))["campaign"]["campaign_id"])' "${CONFIG}")"
REVIEW_ARGS=()
if [[ -n "${REVIEW_TRACE_COUNT:-}" ]]; then
  REVIEW_ARGS+=(--review-trace-count "${REVIEW_TRACE_COUNT}")
fi
if [[ -n "${REVIEW_TRACE_COUNT_JUSTIFICATION:-}" ]]; then
  REVIEW_ARGS+=(
    --review-trace-count-justification
    "${REVIEW_TRACE_COUNT_JUSTIFICATION}"
  )
fi
"${PYTHON_BIN}" -m study.causal.calibration variance \
  --input-root "study/output/${CAMPAIGN_ID}/causal/completed" \
  --config "${CONFIG}" \
  "${REVIEW_ARGS[@]}" \
  --output study/native_gates/calibration/variance_calibration_report.json
"${PYTHON_BIN}" -m study.causal.reports calibration \
  --input study/native_gates/calibration/variance_calibration_report.json \
  --markdown study/native_gates/calibration/NATIVE_VARIANCE_REPORT.md

echo "Review the n recommendation. Re-run with REVIEW_TRACE_COUNT=25 or 50; if overriding the proposal, also set REVIEW_TRACE_COUNT_JUSTIFICATION."
