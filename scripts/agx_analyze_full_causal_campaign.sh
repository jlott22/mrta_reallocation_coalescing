#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_agx_causal_common.sh"

CONFIG="${1:-study/frozen/native_causal_v1/agx_full_causal_frozen.json}"
require_file "${CONFIG}"
OUTPUT_ROOT="$(${PYTHON_BIN} -c 'import json,sys; print(json.load(open(sys.argv[1]))["campaign"]["output_root"])' "${CONFIG}")"
"${PYTHON_BIN}" -m study.causal.analysis \
  --config "${CONFIG}" \
  --repo-root . \
  --causal-root "${OUTPUT_ROOT}/causal/completed" \
  --zero-compute-root "${OUTPUT_ROOT}/zero_compute/completed" \
  --output-dir "${OUTPUT_ROOT}/analysis"
"${PYTHON_BIN}" -m study.causal.reports full \
  --config "${CONFIG}" \
  --repo-root . \
  --campaign-root "${OUTPUT_ROOT}" \
  --analysis-root "${OUTPUT_ROOT}/analysis" \
  --json "${OUTPUT_ROOT}/full_campaign_report.json" \
  --markdown "${OUTPUT_ROOT}/FULL_CAMPAIGN_REPORT.md"
