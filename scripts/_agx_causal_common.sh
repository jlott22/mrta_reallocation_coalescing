#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

export PYTHONPATH="${REPO_ROOT}/Simulation/Architecture${PYTHONPATH:+:${PYTHONPATH}}"
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1
export BLIS_NUM_THREADS=1
export PYTHONHASHSEED=0

PYTHON_BIN="${PYTHON_BIN:-python3}"

require_file() {
  if [[ ! -f "$1" ]]; then
    echo "Required file is missing: $1" >&2
    exit 2
  fi
}

run_causal_with_tracker() {
  if [[ $# -lt 1 ]]; then
    echo "run_causal_with_tracker requires a config path" >&2
    return 2
  fi
  local config="$1"
  shift
  local output_root
  local tracker_path
  local tracker_pid
  local orchestrator_status
  output_root="$(${PYTHON_BIN} -c 'import json,sys; print(json.load(open(sys.argv[1]))["campaign"]["output_root"])' "${config}")"
  tracker_path="${REPO_ROOT}/${output_root}/LIVE_TRACKER.md"
  mkdir -p "${REPO_ROOT}/${output_root}"
  "${PYTHON_BIN}" -m study.causal.tracker \
    --repo-root "${REPO_ROOT}" \
    --config "${config}" \
    --output "${tracker_path}" \
    --watch \
    "$@" &
  tracker_pid=$!
  echo "Live tracker: ${tracker_path}"
  if "${PYTHON_BIN}" -m study.causal.orchestrator \
    --repo-root "${REPO_ROOT}" --config "${config}" "$@"; then
    orchestrator_status=0
  else
    orchestrator_status=$?
  fi
  kill -TERM "${tracker_pid}" 2>/dev/null || true
  wait "${tracker_pid}" 2>/dev/null || true
  # Render one final snapshot after the orchestrator has flushed its events.
  "${PYTHON_BIN}" -m study.causal.tracker \
    --repo-root "${REPO_ROOT}" \
    --config "${config}" \
    --output "${tracker_path}" \
    "$@" >/dev/null 2>&1 || true
  return "${orchestrator_status}"
}
