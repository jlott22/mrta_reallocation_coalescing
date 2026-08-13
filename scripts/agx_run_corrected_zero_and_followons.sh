#!/usr/bin/env bash
set -u -o pipefail

ROOT="/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_aug14_v1/fix_worktree"
CONTROL="/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_aug14_v1"
LOG="${CONTROL}/corrected_zero_and_followons.log"
LOCK="${CONTROL}/corrected_zero_and_followons.lock"
PID_FILE="${CONTROL}/corrected_zero_and_followons.pid"
STATE="${CONTROL}/corrected_agx_state.json"
TRACKER="${ROOT}/scripts/agx_corrected_followon_tracker.py"
MAIN_TRACKER="/home/agxorin/mrta_reallocation_coalescing/scripts/agx_deadline_tracker.py"

exec 7>"${LOCK}"
if ! flock -n 7; then
  echo "A corrected AGX follow-on supervisor already owns ${LOCK}" >&2
  exit 3
fi
printf '%s\n' "$$" > "${PID_FILE}"
tracker_pid=""
cleanup() {
  if [[ -n "${tracker_pid}" ]]; then
    kill "${tracker_pid}" 2>/dev/null || true
  fi
  rm -f -- "${PID_FILE}"
}
trap cleanup EXIT INT TERM
exec >>"${LOG}" 2>&1

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
  local stage="$1"
  local message="$2"
  python3 - "${STATE}" "${stage}" "${message}" <<'PY'
import json, os, sys, time
from pathlib import Path
path = Path(sys.argv[1])
try:
    value = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    value = {"started_epoch": time.time()}
value.update({"stage": sys.argv[2], "message": sys.argv[3], "updated_epoch": time.time()})
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY
}

hardware_invariant() {
  local hardware_pid="751040"
  if ! kill -0 "${hardware_pid}" 2>/dev/null; then
    echo "Hardware orchestrator ${hardware_pid} is not live; stopping AGX follow-ons" >&2
    return 1
  fi
  return 0
}

fits_before_hardware() {
  local required_hours="$1"
  python3 - "${CONTROL}/LIVE_TRACKER.md" "${required_hours}" <<'PY'
import datetime as dt
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r"Estimated finish \(UTC\): \*\*([^*]+)\*\*", text)
if match is None or match.group(1) == "measuring":
    raise SystemExit(1)
finish = dt.datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
remaining_hours = (finish - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600.0
raise SystemExit(0 if remaining_hours >= float(sys.argv[2]) else 1)
PY
}

run_stage() {
  local label="$1"
  local config="$2"
  local output="$3"
  local planned="$4"
  local attempt
  local failed=1
  write_state "${label}" "Running ${planned} balanced AGX-only missions on CPUs 3-8"
  hardware_invariant || return 1
  for attempt in 1 2; do
    if taskset -c 3-8 python3 -m study.campaign --config "${config}" --max-workers 6; then
      failed=0
      break
    fi
    echo "${label} attempt ${attempt}/2 retained failures; completed jobs preserved"
  done
  taskset -c 3-8 python3 -m study.analysis \
    --config "${config}" --analysis-dir "${output}/analysis" || true
  if [[ ${failed} -ne 0 ]]; then
    echo "${label} retained unresolved failures after two passes; continuing the queue"
  fi
  hardware_invariant
}

write_state "starting" "Validating clean corrected source before launch"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing corrected campaign from a dirty worktree" >&2
  exit 4
fi
python3 "${TRACKER}" --interval 30 &
tracker_pid="$!"
python3 "${MAIN_TRACKER}" set --pipeline agx --stage corrected_zero_480 \
  --status running --message "Launching corrected zero-compute controls on CPUs 3-8"

failed=0
run_stage corrected_zero_480 \
  configs/agx_deadline_zero_compute_480_scheduler_v2.json \
  study/output/agx_deadline_zero_compute_480_scheduler_v2 480 || failed=1
if fits_before_hardware 4; then
  run_stage arrival_verify_additional \
    configs/agx_deadline_arrival_verify_traces_1_2_v2.json \
    study/output/agx_deadline_arrival_verify_traces_1_2_v2 48 || failed=1
  run_stage timeout_verify_additional \
    configs/agx_deadline_timeout_verify_traces_1_2_v2.json \
    study/output/agx_deadline_timeout_verify_traces_1_2_v2 48 || failed=1
else
  write_state verification_skipped_for_deadline \
    "Hardware ETA has less than the four-hour safety allowance; additional verification skipped"
fi
if fits_before_hardware 18; then
  run_stage full_zero_extension \
    configs/agx_deadline_zero_compute_traces_8_24_v2.json \
    study/output/agx_deadline_zero_compute_traces_8_24_v2 1020 || failed=1
else
  write_state full_zero_extension_skipped_for_deadline \
    "Hardware ETA has less than the 18-hour extension allowance; zero-compute extension skipped"
fi

if [[ ${failed} -eq 0 ]]; then
  write_state complete "Corrected 480, restored three-trace verification, and full 25-trace zero-compute suite finished"
  python3 "${MAIN_TRACKER}" set --pipeline agx --stage complete --status complete \
    --message "Corrected zero-compute and deadline-safe AGX follow-ons complete"
else
  write_state complete_with_retained_failures "All stages executed; inspect retained failures"
  python3 "${MAIN_TRACKER}" set --pipeline agx --stage complete_with_retained_failures \
    --status complete --message "All corrected AGX stages executed; retained failures remain logged"
fi
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) corrected AGX queue finished status=${failed}"
exit "${failed}"
