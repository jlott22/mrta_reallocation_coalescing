#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_agx_causal_common.sh"

if [[ $# -ne 1 ]]; then
  echo "Usage: bash scripts/agx_prepare_rp2040_boards.sh PORT_A,PORT_B,PORT_C" >&2
  echo "Use stable /dev/serial/by-id paths where available." >&2
  exit 2
fi

PORTS="$1"
mkdir -p study/native_gates/device_build configs/local
BUILD_REPORT="study/native_gates/device_build/device_build_deployment.json"

"${PYTHON_BIN}" -m pip install -r Simulation/Architecture/allocator_replay/requirements-host.txt
"${PYTHON_BIN}" -m study.causal.native --repo-root "${REPO_ROOT}" \
  build-deploy \
  --ports "${PORTS}" \
  --deploy \
  --output "${BUILD_REPORT}"

BUILD_ROOT="$(${PYTHON_BIN} -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["build_root"])' "${BUILD_REPORT}")"
"${PYTHON_BIN}" -m study.causal.native --repo-root "${REPO_ROOT}" \
  discover-bindings \
  --ports "${PORTS}" \
  --build-root "${BUILD_ROOT}" \
  --core-affinities "${CAUSAL_CORE_AFFINITIES:-0,1,2}" \
  --output configs/local/agx_board_bindings.json

echo "Created sealed local binding: configs/local/agx_board_bindings.json"
echo "No motors, sensors, or main.py were initialized or replaced."
