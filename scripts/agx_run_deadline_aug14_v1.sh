#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ROOT="${REPO_ROOT}/study/output/agx_deadline_aug14_v1"
TRACKER_SCRIPT="${REPO_ROOT}/scripts/agx_deadline_tracker.py"
FINALIZER="${REPO_ROOT}/scripts/agx_finalize_deadline_aug14_v1.py"
TRACKER_FILE="${RUN_ROOT}/LIVE_TRACKER.md"
LOG="${RUN_ROOT}/unattended.log"
AGX_LOG="${RUN_ROOT}/agx_pipeline.log"
HARDWARE_LOG="${RUN_ROOT}/hardware_pipeline.log"
LOCK_FILE="${RUN_ROOT}/runner.lock"
PID_FILE="${RUN_ROOT}/runner.pid"
CORE_CONFIG="configs/agx_deadline_hardware_core_96_v1.json"
BOUNDED_CONFIG="configs/agx_deadline_hardware_bounded_8_v1.json"
PRIMARY_CONFIG="configs/agx_deadline_primary_1396_v1.json"
ARRIVAL_CONFIG="configs/agx_deadline_arrival_verify_24_v1.json"
TIMEOUT_CONFIG="configs/agx_deadline_timeout_verify_24_v1.json"
ZERO_CONFIG="configs/agx_deadline_zero_compute_480_v1.json"
PORTS="/dev/serial/by-id/usb-Pololu_Corporation_Pololu_3pi+_2040_Robot_MicroPython_e4621cb30b16392f-if00,/dev/serial/by-id/usb-Pololu_Corporation_Pololu_3pi+_2040_Robot_MicroPython_e4621cb30b43372f-if00,/dev/serial/by-id/usb-Pololu_Corporation_Pololu_3pi+_2040_Robot_MicroPython_e4621cb30b4b372f-if00"
MODE="${1:---prepare-and-run}"

case "${MODE}" in
  --prepare-and-run|--resume)
    ;;
  *)
    echo "Usage: bash scripts/agx_run_deadline_aug14_v1.sh [--prepare-and-run|--resume]" >&2
    exit 2
    ;;
esac

cd "${REPO_ROOT}"
export PYTHONPATH="${REPO_ROOT}/Simulation/Architecture${PYTHONPATH:+:${PYTHONPATH}}"
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1
export BLIS_NUM_THREADS=1
export PYTHONHASHSEED=0

mkdir -p "${RUN_ROOT}"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "Another deadline runner owns ${LOCK_FILE}" >&2
  exit 3
fi
if [[ -s "${PID_FILE}" ]]; then
  prior_pid="$(tr -cd '0-9' < "${PID_FILE}")"
  if [[ -n "${prior_pid}" && "${prior_pid}" != "$$" ]] && kill -0 "${prior_pid}" 2>/dev/null; then
    prior_command="$(tr '\0' ' ' < "/proc/${prior_pid}/cmdline" 2>/dev/null || true)"
    if [[ "${prior_command}" == *"agx_run_deadline_aug14_v1.sh"* ]]; then
      echo "Validated prior deadline runner PID ${prior_pid} is still active" >&2
      exit 3
    fi
  fi
fi
printf '%s\n' "$$" > "${PID_FILE}"
exec > >(tee -a "${LOG}") 2>&1
echo "Live tracker: ${TRACKER_FILE}"
echo "Supervisor log: ${LOG}"
echo "AGX log: ${AGX_LOG}"
echo "Hardware log: ${HARDWARE_LOG}"

source scripts/_agx_causal_common.sh

TRACKER_PID=""
AGX_PID=""
HARDWARE_PID=""
on_signal() {
  exit 143
}
on_exit() {
  local code=$?
  trap - EXIT INT TERM
  for child in "${AGX_PID}" "${HARDWARE_PID}"; do
    if [[ -n "${child}" ]] && kill -0 "${child}" 2>/dev/null; then
      kill -TERM "${child}" 2>/dev/null || true
    fi
  done
  if [[ ${code} -ne 0 ]]; then
    python3 "${TRACKER_SCRIPT}" set --pipeline overall --status failed \
      --message "Deadline runner stopped with exit ${code}; inspect ${LOG}" || true
  fi
  if [[ -n "${TRACKER_PID}" ]]; then
    kill -TERM "${TRACKER_PID}" 2>/dev/null || true
    wait "${TRACKER_PID}" 2>/dev/null || true
  fi
  rm -f -- "${PID_FILE}"
  exit "${code}"
}
trap on_signal INT TERM
trap on_exit EXIT

tracker_set() {
  python3 "${TRACKER_SCRIPT}" set "$@"
}

GIT_COMMIT="$(git rev-parse HEAD)"
tracker_set --pipeline overall --status running --runner-pid "$$" \
  --git-commit "${GIT_COMMIT}" \
  --message "Validating sealed source and the 9-of-12-core execution contract"
tracker_set --pipeline agx --stage pending --status pending \
  --message "Waiting for launch validation"
tracker_set --pipeline hardware --stage pending --status pending \
  --message "Waiting for launch validation"
python3 "${TRACKER_SCRIPT}" watch --interval 30 &
TRACKER_PID=$!

if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "Refusing deadline run from a dirty source tree" >&2
  exit 2
fi
if [[ "$(getconf _NPROCESSORS_ONLN)" -ne 12 || "$(cat /sys/devices/system/cpu/online)" != "0-11" ]]; then
  echo "Deadline plan requires exactly 12 online logical cores" >&2
  exit 2
fi
if ! taskset -c 3-8 true; then
  echo "Cannot establish the AGX CPU 3-8 affinity set" >&2
  exit 2
fi
IFS=',' read -r -a DEVICE_PATHS <<< "${PORTS}"
if [[ ${#DEVICE_PATHS[@]} -ne 3 ]]; then
  echo "Exactly three explicit Pololu paths are required" >&2
  exit 2
fi
for device in "${DEVICE_PATHS[@]}"; do
  if [[ ! -r "${device}" || ! -w "${device}" ]]; then
    echo "Configured Pololu path is not readable/writable: ${device}" >&2
    exit 2
  fi
done

archive_generated_file() {
  local path="$1"
  local archive_dir="$2"
  local digest
  local name
  local stamp
  if [[ ! -e "${path}" ]]; then
    return 0
  fi
  digest="$(sha256sum "${path}" | awk '{print substr($1, 1, 12)}')"
  name="$(basename -- "${path}")"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "${archive_dir}"
  mv -- "${path}" "${archive_dir}/${stamp}_${digest}_${name}"
}

preserve_current_build_bundle() {
  local report="study/native_gates/device_build/device_build_deployment.json"
  local build_root
  local build_id
  local archive_root
  if [[ ! -f "${report}" ]]; then
    return 0
  fi
  build_root="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["build_root"])' "${report}")"
  build_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["build_manifest"]["build_id"])' "${report}")"
  archive_root="study/native_gates/device_build/history/builds/${build_id}"
  if [[ -d "${build_root}" && ! -e "${archive_root}" ]]; then
    mkdir -p "$(dirname -- "${archive_root}")"
    cp -a -- "${build_root}" "${archive_root}"
  fi
}

run_software_validation() {
  python3 -m unittest discover -s known_visit_sim/tests -p 'test_*.py'
  python3 -m unittest discover -s study/tests -p 'test_*.py'
  python3 -m unittest discover -s Tests/HIL/AllocatorReplay -p 'test_*.py'
  python3 -m compileall -q \
    known_visit_sim study Simulation/Architecture/allocator_replay Tests/HIL/AllocatorReplay scripts
  while IFS= read -r -d '' script; do
    bash -n "${script}"
  done < <(find scripts -maxdepth 1 -type f -name '*.sh' -print0)
  git diff --check
}

prepare_current_source_and_boards() {
  tracker_set --pipeline overall --status running \
    --message "Running full software validation before the concurrent deadline launch"
  run_software_validation
  tracker_set --pipeline overall --status running \
    --message "Deploying motor-free modules and regenerating current environment/preflight gates"
  preserve_current_build_bundle
  archive_generated_file \
    study/native_gates/device_build/device_build_deployment.json \
    study/native_gates/device_build/history
  archive_generated_file \
    configs/local/agx_board_bindings.json \
    configs/local/history
  archive_generated_file \
    study/native_gates/preflight/native_preflight_report.json \
    study/native_gates/preflight/history
  archive_generated_file \
    study/native_gates/preflight/NATIVE_PREFLIGHT_REPORT.md \
    study/native_gates/preflight/history
  bash scripts/agx_prepare_rp2040_boards.sh "${PORTS}"
  ACCEPT_RECORDED_POWER_CLOCK_STATE=YES \
    bash scripts/agx_native_environment_check.sh "${CORE_CONFIG}"
  bash scripts/agx_rp2040_preflight.sh "${CORE_CONFIG}"
}

run_sim_stage() {
  local config="$1"
  local analysis_dir="$2"
  local attempt
  for attempt in 1 2 3; do
    if taskset -c 3-8 python3 -m study.campaign \
      --config "${config}" --max-workers 6; then
      taskset -c 3-8 python3 -m study.analysis \
        --config "${config}" --analysis-dir "${analysis_dir}"
      return 0
    fi
    echo "AGX stage ${config} attempt ${attempt}/3 incomplete; valid completions retained" >&2
  done
  return 1
}

run_native_stage() {
  local config="$1"
  local completed_root="$2"
  local analysis_dir="$3"
  local attempt
  for attempt in 1 2 3; do
    python3 -m study.causal.native --repo-root "${REPO_ROOT}" \
      recover-stale-locks --config "${config}"
    if run_causal_with_tracker "${config}"; then
      python3 -m study.causal.analysis \
        --config "${config}" \
        --causal-root "${completed_root}" \
        --output-dir "${analysis_dir}" \
        --repo-root .
      return 0
    fi
    echo "Hardware stage ${config} attempt ${attempt}/3 incomplete; valid completions retained" >&2
  done
  return 1
}

agx_pipeline() {
  tracker_set --pipeline agx --stage agx_primary --status running \
    --message "Six rolling workers running the 1,396 non-hardware primary cells on CPUs 3-8"
  if ! run_sim_stage "${PRIMARY_CONFIG}" study/output/agx_deadline_primary_1396_v1/analysis; then
    tracker_set --pipeline agx --stage agx_primary --status failed \
      --message "Primary AGX stage exhausted bounded retries"
    return 1
  fi
  tracker_set --pipeline agx --stage arrival_verification --status running \
    --message "Running the balanced one-trace arrival-regime verification"
  if ! run_sim_stage "${ARRIVAL_CONFIG}" study/output/agx_deadline_arrival_verify_24_v1/analysis; then
    tracker_set --pipeline agx --stage arrival_verification --status failed \
      --message "Arrival verification exhausted bounded retries"
    return 1
  fi
  tracker_set --pipeline agx --stage timeout_verification --status running \
    --message "Running the balanced one-trace W=5 timeout verification"
  if ! run_sim_stage "${TIMEOUT_CONFIG}" study/output/agx_deadline_timeout_verify_24_v1/analysis; then
    tracker_set --pipeline agx --stage timeout_verification --status failed \
      --message "Timeout verification exhausted bounded retries"
    return 1
  fi
  tracker_set --pipeline agx --stage zero_compute --status running \
    --message "Running 480 zero-compute controls, eight matched traces per condition"
  if ! run_sim_stage "${ZERO_CONFIG}" study/output/agx_deadline_zero_compute_480_v1/analysis; then
    tracker_set --pipeline agx --stage zero_compute --status failed \
      --message "Zero-compute stage exhausted bounded retries"
    return 1
  fi
  tracker_set --pipeline agx --stage complete --status complete \
    --message "All 1,924 AGX-only primary, verification, and zero-compute missions completed"
}

hardware_pipeline() {
  tracker_set --pipeline hardware --stage hardware_core --status running \
    --message "Running 96 primary Eager/B4 missions; first paired trace is the retained canary"
  if ! run_native_stage \
    "${CORE_CONFIG}" \
    study/output/agx_deadline_hardware_core_96_v1/causal/completed \
    study/output/agx_deadline_hardware_core_96_v1/analysis; then
    tracker_set --pipeline hardware --stage hardware_core --status failed \
      --message "Core hardware stage exhausted bounded retries"
    return 1
  fi
  tracker_set --pipeline hardware --stage hardware_bounded --status running \
    --message "Running eight bounded B4/W5 primary hardware missions"
  if ! run_native_stage \
    "${BOUNDED_CONFIG}" \
    study/output/agx_deadline_hardware_bounded_8_v1/causal/completed \
    study/output/agx_deadline_hardware_bounded_8_v1/analysis; then
    tracker_set --pipeline hardware --stage hardware_bounded --status failed \
      --message "Bounded hardware stage exhausted bounded retries"
    return 1
  fi
  tracker_set --pipeline hardware --stage complete --status complete \
    --message "All 104 hardware-backed primary missions completed"
}

if [[ "${MODE}" == "--prepare-and-run" ]]; then
  prepare_current_source_and_boards
else
  tracker_set --pipeline overall --status running \
    --message "Resume mode: current gates will be revalidated by each hardware stage"
fi

tracker_set --pipeline overall --status running \
  --message "Concurrent deadline pipelines active: hardware CPUs 0-2, AGX CPUs 3-8"
hardware_pipeline >> "${HARDWARE_LOG}" 2>&1 &
HARDWARE_PID=$!
agx_pipeline >> "${AGX_LOG}" 2>&1 &
AGX_PID=$!

if wait "${AGX_PID}"; then
  AGX_STATUS=0
else
  AGX_STATUS=$?
fi
AGX_PID=""
if wait "${HARDWARE_PID}"; then
  HARDWARE_STATUS=0
else
  HARDWARE_STATUS=$?
fi
HARDWARE_PID=""
if [[ ${AGX_STATUS} -ne 0 || ${HARDWARE_STATUS} -ne 0 ]]; then
  echo "Pipeline failure: agx=${AGX_STATUS} hardware=${HARDWARE_STATUS}" >&2
  exit 1
fi

python3 "${FINALIZER}"
tracker_set --pipeline overall --status complete \
  --message "All 2,028 planned executions and terminal analyses completed"
trap - EXIT INT TERM
kill -TERM "${TRACKER_PID}" 2>/dev/null || true
wait "${TRACKER_PID}" 2>/dev/null || true
rm -f -- "${PID_FILE}"
