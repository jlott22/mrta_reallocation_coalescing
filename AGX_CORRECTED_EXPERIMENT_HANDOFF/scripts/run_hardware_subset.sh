#!/usr/bin/env bash
set -euo pipefail

HANDOFF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd -- "${HANDOFF_DIR}/.." && pwd)"
CONFIG="${HANDOFF_DIR}/configs/hardware_core_96.json"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "${REPO_ROOT}"
source scripts/_agx_causal_common.sh

if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "Refusing hardware work from a dirty checkout." >&2
  exit 2
fi
require_file "${CONFIG}"
require_file configs/local/agx_board_bindings.json

# Board deployment, environment attestation, and the brief RP preflight are
# intentionally separate prerequisites; this script does not repeat them.
"${PYTHON_BIN}" -m study.causal.native --repo-root "${REPO_ROOT}" \
  recover-stale-locks --config "${CONFIG}"

run_causal_with_tracker "${CONFIG}"

OUTPUT_ROOT="$(${PYTHON_BIN} -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["campaign"]["output_root"])' "${CONFIG}")"
"${PYTHON_BIN}" -m study.causal.analysis \
  --config "${CONFIG}" --repo-root "${REPO_ROOT}" \
  --causal-root "${OUTPUT_ROOT}/causal/completed" \
  --output-dir "${OUTPUT_ROOT}/analysis"
