#!/usr/bin/env bash
set -u -o pipefail

ROOT="/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_aug14_v1/fix_worktree"
RUN_ROOT="${ROOT}/study/output/agx_n50_extension_v1"
LOCK="${RUN_ROOT}/runner.lock"
PID_FILE="${RUN_ROOT}/runner.pid"
STATE="${RUN_ROOT}/state.json"
SUPERVISOR_LOG="${RUN_ROOT}/supervisor.log"
TRACKER="${ROOT}/scripts/agx_n50_extension_tracker.py"

mkdir -p "${RUN_ROOT}"
exec 7>"${LOCK}"
if ! flock -n 7; then
  echo "An n=50 AGX extension supervisor already owns ${LOCK}" >&2
  exit 3
fi
printf '%s\n' "$$" > "${PID_FILE}"
tracker_pid=""
cleanup() {
  if [[ -n "${tracker_pid}" ]]; then
    kill "${tracker_pid}" 2>/dev/null || true
    wait "${tracker_pid}" 2>/dev/null || true
  fi
  python3 "${TRACKER}" --once 2>/dev/null || true
  rm -f -- "${PID_FILE}"
}
trap cleanup EXIT INT TERM
exec >>"${SUPERVISOR_LOG}" 2>&1

cd "${ROOT}"
export PYTHONPATH="${ROOT}/Simulation/Architecture${PYTHONPATH:+:${PYTHONPATH}}"
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1
export BLIS_NUM_THREADS=1
export PYTHONHASHSEED=0

write_state() {
  local status="$1"
  local stage="$2"
  local message="$3"
  python3 - "${STATE}" "${status}" "${stage}" "${message}" "$$" "$(git rev-parse HEAD)" <<'PY'
import json, os, sys, time
from pathlib import Path
path = Path(sys.argv[1])
try:
    value = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    value = {"started_epoch": time.time()}
value.update({
    "status": sys.argv[2],
    "stage": sys.argv[3],
    "message": sys.argv[4],
    "supervisor_pid": int(sys.argv[5]),
    "git_commit": sys.argv[6],
    "updated_epoch": time.time(),
})
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY
  python3 "${TRACKER}" --once
}

run_stage() {
  local stage="$1"
  local config="$2"
  local output="$3"
  local log="$4"
  local attempt
  local passed=1
  write_state running "${stage}" "Running 1,500 new-trace ${stage} trials on CPUs 3-8"
  for attempt in 1 2; do
    if taskset -c 3-8 python3 -m study.campaign \
      --config "${config}" --max-workers 6 >>"${log}" 2>&1; then
      passed=0
      break
    fi
    echo "${stage} invocation ${attempt}/2 retained failures; valid completions preserved" >>"${log}"
  done
  taskset -c 3-8 python3 -m study.analysis \
    --config "${config}" --analysis-dir "${output}/analysis" >>"${log}" 2>&1 || true
  if [[ ${passed} -ne 0 ]]; then
    echo "${stage} has unresolved retained failures; continuing to the paired stage" >>"${log}"
  fi
  return "${passed}"
}

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing n=50 extension from a dirty worktree" >&2
  exit 4
fi
write_state running starting "Starting isolated n=50 extension"
python3 "${TRACKER}" --interval 30 &
tracker_pid="$!"

failed=0
run_stage agx_causal_new_traces \
  configs/agx_n50_extension_causal_1500_v1.json \
  study/output/agx_n50_extension_causal_1500_v1 \
  "${RUN_ROOT}/agx_causal.log" || failed=1
run_stage zero_compute_new_traces \
  configs/agx_n50_extension_zero_compute_1500_v1.json \
  study/output/agx_n50_extension_zero_compute_1500_v1 \
  "${RUN_ROOT}/zero_compute.log" || failed=1

if [[ ${failed} -eq 0 ]]; then
  write_state complete complete "Both 1,500-trial new-trace stages completed and analyzed"
else
  write_state complete_with_retained_failures complete_with_retained_failures \
    "Both stages executed and analyzed; retained failures remain logged"
fi
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) n=50 extension finished status=${failed}"
exit "${failed}"
