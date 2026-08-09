#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/_agx_causal_common.sh"

CONFIG="${1:-study/frozen/native_causal_v1/agx_full_causal_frozen.json}"
require_file "${CONFIG}"
"${PYTHON_BIN}" -m study.causal.orchestrator --repo-root "${REPO_ROOT}" --config "${CONFIG}"
