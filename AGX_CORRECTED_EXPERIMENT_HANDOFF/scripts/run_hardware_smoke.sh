#!/usr/bin/env bash
set -euo pipefail

HANDOFF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd -- "${HANDOFF_DIR}/.." && pwd)"
CONFIG="${HANDOFF_DIR}/configs/rp2040_smoke_8.json"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "${REPO_ROOT}"
source scripts/_agx_causal_common.sh

if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "Refusing hardware smoke from a dirty checkout." >&2
  exit 2
fi
"${PYTHON_BIN}" "${HANDOFF_DIR}/scripts/validate_topology.py" \
  --repo-root "${REPO_ROOT}" --require-hardware-bindings
require_file "${CONFIG}"
require_file configs/local/agx_board_bindings.json

"${PYTHON_BIN}" -m study.causal.native --repo-root "${REPO_ROOT}" \
  recover-stale-locks --config "${CONFIG}"

# The second invocation proves content-validated resume without recomputing the
# eight engineering-only missions. The generated report is the fresh smoke gate
# required by the fixed 96-mission hardware subset.
run_causal_with_tracker "${CONFIG}"
run_causal_with_tracker "${CONFIG}"

OUTPUT_ROOT="$("${PYTHON_BIN}" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["campaign"]["output_root"])' "${CONFIG}")"
mkdir -p study/native_gates/smoke
"${PYTHON_BIN}" -m study.causal.reports smoke \
  --config "${CONFIG}" --repo-root "${REPO_ROOT}" \
  --campaign-root "${REPO_ROOT}/${OUTPUT_ROOT}" \
  --json study/native_gates/smoke/causal_smoke_report.json \
  --markdown study/native_gates/smoke/NATIVE_CAUSAL_SMOKE_REPORT.md
