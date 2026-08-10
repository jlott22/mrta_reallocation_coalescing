#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_agx_causal_common.sh"

CONFIG="${1:-study/frozen/native_causal_v1/agx_full_causal_frozen.json}"
require_file "${CONFIG}"
run_causal_with_tracker "${CONFIG}" --zero-compute
