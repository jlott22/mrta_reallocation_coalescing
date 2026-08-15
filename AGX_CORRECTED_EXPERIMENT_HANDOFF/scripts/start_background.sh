#!/usr/bin/env bash
set -euo pipefail

HANDOFF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd -- "${HANDOFF_DIR}/.." && pwd)"
RUN_ROOT="${REPO_ROOT}/study/output/corrected_experiment_supervisor_v6"
START_HARDWARE="${START_HARDWARE:-NO}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "${REPO_ROOT}"
mkdir -p "${RUN_ROOT}"

if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "Refusing to launch from a dirty checkout. Use a fresh clone of main." >&2
  exit 2
fi
if (( $(getconf _NPROCESSORS_ONLN) < 12 )); then
  echo "The handoff requires at least 12 online logical cores." >&2
  exit 2
fi

if [[ "${START_HARDWARE}" == "YES" ]]; then
  "${PYTHON_BIN}" "${HANDOFF_DIR}/scripts/validate_topology.py" \
    --repo-root "${REPO_ROOT}" --require-hardware-bindings
else
  "${PYTHON_BIN}" "${HANDOFF_DIR}/scripts/validate_topology.py" \
    --repo-root "${REPO_ROOT}"
fi
RP2040_CORE_SET="$("${PYTHON_BIN}" -c 'import json,sys; print(",".join(map(str, json.load(open(sys.argv[1], encoding="utf-8"))["rp2040"]["worker_cores"])))' "${HANDOFF_DIR}/experiment_matrix.json")"
SUPERVISOR_CORE="$("${PYTHON_BIN}" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["support_cores"]["supervisor"])' "${HANDOFF_DIR}/experiment_matrix.json")"

launch_once() {
  local name="$1"
  local cores="$2"
  local script="$3"
  local pid_file="${RUN_ROOT}/${name}.pid"
  local log_file="${RUN_ROOT}/${name}.log"
  if [[ -s "${pid_file}" ]]; then
    local prior_pid
    prior_pid="$(tr -cd '0-9' < "${pid_file}")"
    if [[ -n "${prior_pid}" ]] && kill -0 "${prior_pid}" 2>/dev/null; then
      echo "${name} is already running as PID ${prior_pid}."
      return 0
    fi
  fi
  nohup taskset -c "${cores}" bash "${script}" >"${log_file}" 2>&1 &
  printf '%s\n' "$!" > "${pid_file}"
  echo "Started ${name}: PID $!, log ${log_file}"
}

# The supervisor owns CPU 9; each simulation campaign it launches owns CPUs 4-8.
launch_once simulation "${SUPERVISOR_CORE}" "${HANDOFF_DIR}/scripts/run_simulation_rounds.sh"

if [[ "${START_HARDWARE}" == "YES" ]]; then
  # The four-board orchestrator and its workers inherit the CPU 0-3 affinity set.
  launch_once hardware "${RP2040_CORE_SET}" "${HANDOFF_DIR}/scripts/run_hardware_subset.sh"
else
  echo "Hardware subset not started. Set START_HARDWARE=YES when four prepared boards are connected."
fi

echo "The launch is detached; no AI or terminal session must remain open."
