#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_agx_causal_common.sh"

CONFIG="${1:-configs/agx_causal_smoke_v1.json}"
require_file "${CONFIG}"
OUTPUT_ROOT="$(${PYTHON_BIN} -c 'import json,sys; print(json.load(open(sys.argv[1]))["campaign"]["output_root"])' "${CONFIG}")"
"${PYTHON_BIN}" -m study.causal.orchestrator --repo-root "${REPO_ROOT}" --config "${CONFIG}"
# A second invocation is intentional: the smoke gate requires content-validated resume.
"${PYTHON_BIN}" -m study.causal.orchestrator --repo-root "${REPO_ROOT}" --config "${CONFIG}"
mkdir -p study/native_gates/smoke
"${PYTHON_BIN}" -m study.causal.reports smoke \
  --config "${CONFIG}" \
  --repo-root . \
  --campaign-root "${OUTPUT_ROOT}" \
  --json study/native_gates/smoke/causal_smoke_report.json \
  --markdown study/native_gates/smoke/NATIVE_CAUSAL_SMOKE_REPORT.md
