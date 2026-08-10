#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ROOT="${REPO_ROOT}/study/output/final_minimal_unattended_v2"
MONITOR="${REPO_ROOT}/scripts/agx_final_minimal_tracker.py"
FINALIZER="${REPO_ROOT}/scripts/agx_finalize_final_minimal_v2.py"
TRACKER_FILE="${RUN_ROOT}/LIVE_TRACKER.md"
LOG="${RUN_ROOT}/unattended.log"
LOCK_FILE="${RUN_ROOT}/runner.lock"
PID_FILE="${RUN_ROOT}/runner.pid"
SMOKE_CONFIG="configs/agx_causal_smoke_v2.json"
PORTS="/dev/serial/by-id/usb-Pololu_Corporation_Pololu_3pi+_2040_Robot_MicroPython_e4621cb30b16392f-if00,/dev/serial/by-id/usb-Pololu_Corporation_Pololu_3pi+_2040_Robot_MicroPython_e4621cb30b43372f-if00,/dev/serial/by-id/usb-Pololu_Corporation_Pololu_3pi+_2040_Robot_MicroPython_e4621cb30b4b372f-if00"
MODE="${1:---prepare-and-run}"

case "${MODE}" in
  --prepare-and-run|--resume)
    ;;
  *)
    echo "Usage: bash scripts/agx_run_final_minimal_unattended_v2.sh [--prepare-and-run|--resume]" >&2
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
if ! command -v flock >/dev/null 2>&1; then
  echo "flock is required for singleton unattended execution" >&2
  exit 2
fi
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "Another final-minimal runner owns ${LOCK_FILE}" >&2
  exit 3
fi
if [[ -s "${PID_FILE}" ]]; then
  prior_pid="$(tr -cd '0-9' < "${PID_FILE}")"
  if [[ -n "${prior_pid}" && "${prior_pid}" != "$$" ]] && kill -0 "${prior_pid}" 2>/dev/null; then
    prior_command="$(tr '\0' ' ' < "/proc/${prior_pid}/cmdline" 2>/dev/null || true)"
    if [[ "${prior_command}" == *"agx_run_final_minimal_unattended_v2.sh"* ]]; then
      echo "Validated prior runner PID ${prior_pid} is still active" >&2
      exit 3
    fi
  fi
fi
printf '%s\n' "$$" > "${PID_FILE}"
exec > >(tee -a "${LOG}") 2>&1
echo "Live tracker: ${TRACKER_FILE}"
echo "Unattended log: ${LOG}"

source scripts/_agx_causal_common.sh

MONITOR_PID=""
on_signal() {
  exit 143
}
on_exit() {
  local code=$?
  trap - EXIT INT TERM
  if [[ ${code} -ne 0 ]]; then
    python3 "${MONITOR}" set --phase failed --status failed \
      --message "Unattended runner stopped with exit ${code}; inspect ${LOG}" || true
  fi
  if [[ -n "${MONITOR_PID}" ]]; then
    kill -TERM "${MONITOR_PID}" 2>/dev/null || true
    wait "${MONITOR_PID}" 2>/dev/null || true
  fi
  rm -f -- "${PID_FILE}"
  exit "${code}"
}
trap on_signal INT TERM
trap on_exit EXIT

GIT_COMMIT="$(git rev-parse HEAD)"
python3 "${MONITOR}" set --phase initializing --status running \
  --runner-pid "$$" --git-commit "${GIT_COMMIT}" \
  --message "Validating sealed source and singleton three-board runner"
python3 "${MONITOR}" watch --interval 30 &
MONITOR_PID=$!

if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "Refusing unattended publication run from a dirty source tree"
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
    known_visit_sim study Simulation/Architecture/allocator_replay Tests/HIL/AllocatorReplay
  while IFS= read -r -d '' script; do
    bash -n "${script}"
  done < <(find scripts -maxdepth 1 -type f -name '*.sh' -print0)
  git diff --check
}

prepare_current_source_and_boards() {
  python3 "${MONITOR}" set --phase software_validation --status running \
    --message "Running the complete software validation suite before deployment"
  run_software_validation

  python3 "${MONITOR}" set --phase gate_regeneration --status running \
    --message "Building and deploying corrected motor-free modules; regenerating bindings and gates"
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
    bash scripts/agx_native_environment_check.sh "${SMOKE_CONFIG}"
  bash scripts/agx_rp2040_preflight.sh "${SMOKE_CONFIG}"
}

run_sim_stage() {
  local config="$1"
  local analysis_dir="$2"
  local attempt
  for attempt in 1 2 3; do
    if python3 -m study.campaign --config "${config}" --max-workers 4; then
      python3 -m study.analysis --config "${config}" --analysis-dir "${analysis_dir}"
      return 0
    fi
    echo "Simulation stage attempt ${attempt}/3 was incomplete; valid completions are retained for resume" >&2
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
    echo "Native stage attempt ${attempt}/3 was incomplete; retained failures remain visible" >&2
  done
  return 1
}

run_smoke_stage() {
  local config="$1"
  local attempt
  for attempt in 1 2 3; do
    # The recovery command only releases leases whose owning process is proven
    # dead.  Live board owners remain fail-closed.
    python3 -m study.causal.native --repo-root "${REPO_ROOT}" \
      recover-stale-locks --config "${config}"
    # agx_causal_smoke performs the required second invocation after all jobs
    # complete, proving content-validated skip-all resume before writing the
    # canonical smoke gate consumed by the publication configurations.
    if bash scripts/agx_causal_smoke.sh "${config}"; then
      return 0
    fi
    echo "Hardware smoke stage attempt ${attempt}/3 did not pass; retrying only retained pending work" >&2
  done
  return 1
}

if [[ "${MODE}" == "--prepare-and-run" ]]; then
  prepare_current_source_and_boards
else
  python3 "${MONITOR}" set --phase resume_validation --status running \
    --message "Resume mode: preserving current deployment and revalidating existing gates at smoke launch"
fi

python3 "${MONITOR}" set --phase hardware_smoke --status running \
  --message "Running a fresh 16-mission v2 engineering smoke and content-validated resume"
run_smoke_stage "${SMOKE_CONFIG}"

python3 "${MONITOR}" set --phase arrival_verification --status running \
  --message "Running 72 AGX causal arrival-regime verification trials"
run_sim_stage \
  configs/agx_minimal_arrival_verify_v1.json \
  study/output/agx_minimal_arrival_verify_v1/analysis

python3 "${MONITOR}" set --phase timeout_verification --status running \
  --message "Running 72 AGX causal W=5 verification trials"
run_sim_stage \
  configs/agx_minimal_timeout_verify_v1.json \
  study/output/agx_minimal_timeout_verify_v1/analysis

python3 "${MONITOR}" set --phase agx_full_n25 --status running \
  --message "Running the primary 1,500-trial AGX causal factorial"
run_sim_stage \
  configs/agx_minimal_full_n25_v1.json \
  study/output/agx_minimal_full_n25_v1/analysis

python3 "${MONITOR}" set --phase zero_compute --status running \
  --message "Running the 1,500-condition zero-compute counterpart"
run_sim_stage \
  configs/agx_minimal_zero_compute_n25_v1.json \
  study/output/agx_minimal_zero_compute_n25_v1/analysis

python3 "${MONITOR}" set --phase hardware_core --status running \
  --message "Running 96 paired Eager/B4 RP2040 publication missions on three boards"
run_native_stage \
  configs/agx_minimal_hardware_core_96_v1.json \
  study/output/agx_minimal_hardware_core_96_v1/causal/completed \
  study/output/agx_minimal_hardware_core_96_v1/analysis

python3 "${MONITOR}" set --phase hardware_bounded --status running \
  --message "Running 24 bounded-B4-W5 RP2040 publication missions on three boards"
run_native_stage \
  configs/agx_minimal_hardware_bounded_24_v1.json \
  study/output/agx_minimal_hardware_bounded_24_v1/causal/completed \
  study/output/agx_minimal_hardware_bounded_24_v1/analysis

python3 "${FINALIZER}"
python3 "${MONITOR}" set --phase complete --status complete \
  --message "All planned stages and analyses completed"
trap - EXIT INT TERM
kill -TERM "${MONITOR_PID}" 2>/dev/null || true
wait "${MONITOR_PID}" 2>/dev/null || true
rm -f -- "${PID_FILE}"
