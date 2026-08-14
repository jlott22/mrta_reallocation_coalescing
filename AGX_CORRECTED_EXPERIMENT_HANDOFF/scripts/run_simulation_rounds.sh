#!/usr/bin/env bash
set -euo pipefail

HANDOFF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd -- "${HANDOFF_DIR}/.." && pwd)"
RUN_ROOT="${REPO_ROOT}/study/output/corrected_experiment_supervisor"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "${REPO_ROOT}"
mkdir -p "${RUN_ROOT}"
exec 9>"${RUN_ROOT}/simulation.lock"
if ! flock -n 9; then
  echo "Another corrected simulation supervisor is already running." >&2
  exit 3
fi

export PYTHONPATH="${REPO_ROOT}/Simulation/Architecture${PYTHONPATH:+:${PYTHONPATH}}"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 BLIS_NUM_THREADS=1
export PYTHONHASHSEED=0

"${PYTHON_BIN}" "${HANDOFF_DIR}/scripts/validate_topology.py" \
  --repo-root "${REPO_ROOT}"
AGX_CORE_SET="$("${PYTHON_BIN}" -c 'import json,sys; print(",".join(map(str, json.load(open(sys.argv[1], encoding="utf-8"))["agx"]["simulation_cores"])))' "${HANDOFF_DIR}/experiment_matrix.json")"
AGX_WORKER_COUNT="$("${PYTHON_BIN}" -c 'import json,sys; print(len(json.load(open(sys.argv[1], encoding="utf-8"))["agx"]["simulation_cores"]))' "${HANDOFF_DIR}/experiment_matrix.json")"
QA_CORE="$("${PYTHON_BIN}" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["support_cores"]["checkpoint_analysis"])' "${HANDOFF_DIR}/experiment_matrix.json")"

if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "Refusing to run from a dirty checkout." >&2
  exit 2
fi

AGX_SMOKE="${HANDOFF_DIR}/configs/agx_smoke_8.json"
R1_CAUSAL="${HANDOFF_DIR}/configs/round1_causal.json"
R1_ZERO="${HANDOFF_DIR}/configs/round1_zero.json"
R2_CAUSAL="${HANDOFF_DIR}/configs/round2_causal.json"
R2_ZERO="${HANDOFF_DIR}/configs/round2_zero.json"

run_stage() {
  local config="$1"
  local label="$2"
  local output_root
  output_root="$(${PYTHON_BIN} -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["campaign"]["output_root"])' "${config}")"
  echo "[$(date -u +%FT%TZ)] Starting ${label}"
  for attempt in 1 2 3; do
    if taskset -c "${AGX_CORE_SET}" "${PYTHON_BIN}" -m study.campaign \
      --config "${config}" --repo-root "${REPO_ROOT}" --max-workers "${AGX_WORKER_COUNT}"; then
      taskset -c "${QA_CORE}" "${PYTHON_BIN}" -m study.analysis \
        --config "${config}" --repo-root "${REPO_ROOT}" \
        --analysis-dir "${output_root}/analysis"
      echo "[$(date -u +%FT%TZ)] Finished ${label}"
      return 0
    fi
    echo "${label} attempt ${attempt}/3 ended with technical work outstanding; valid jobs remain resumable." >&2
  done
  return 1
}

echo "Running brief software validation once before the matrix."
taskset -c "${QA_CORE}" "${PYTHON_BIN}" -m unittest discover -s known_visit_sim/tests -p 'test_*.py'
taskset -c "${QA_CORE}" "${PYTHON_BIN}" -m unittest discover -s study/tests -p 'test_*.py'
taskset -c "${QA_CORE}" "${PYTHON_BIN}" -m unittest discover -s Tests/HIL/AllocatorReplay -p 'test_*.py'
for config in "${AGX_SMOKE}" "${R1_CAUSAL}" "${R1_ZERO}" "${R2_CAUSAL}" "${R2_ZERO}"; do
  taskset -c "${QA_CORE}" "${PYTHON_BIN}" -m study.campaign \
    --config "${config}" --repo-root "${REPO_ROOT}" --prepare-only
done

run_stage "${AGX_SMOKE}" "Engineering-only AGX smoke (8 jobs)"
run_stage "${R1_CAUSAL}" "Round 1 host-causal (25 trials per condition)"
run_stage "${R1_ZERO}" "Round 1 zero-time (25 trials per condition)"

echo "Running the required Round 1 checkpoint before Round 2."
taskset -c "${QA_CORE}" "${PYTHON_BIN}" "${HANDOFF_DIR}/scripts/verify_round_boundary.py" \
  --repo-root "${REPO_ROOT}" \
  --config "${R1_CAUSAL}" --config "${R1_ZERO}" \
  --round2-config "${R2_CAUSAL}" --round2-config "${R2_ZERO}" \
  --output "${RUN_ROOT}/ROUND1_CHECKPOINT.json"

# The checkpoint is informational: it never produces an allowlist and does not
# remove any algorithm, load, policy, or second-half trace.
run_stage "${R2_CAUSAL}" "Round 2 host-causal (new 25 trials per condition)"
run_stage "${R2_ZERO}" "Round 2 zero-time (new 25 trials per condition)"

printf '%s\n' "complete $(date -u +%FT%TZ)" > "${RUN_ROOT}/SIMULATION_COMPLETE.txt"
