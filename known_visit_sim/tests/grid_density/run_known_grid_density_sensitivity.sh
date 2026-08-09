#!/usr/bin/env bash
set -euo pipefail

WORKERS="${1:-20}"
if ! [[ "$WORKERS" =~ ^[0-9]+$ ]] || [[ "$WORKERS" -lt 1 ]]; then
  echo "Usage: bash known_visit_sim/tests/grid_density/run_known_grid_density_sensitivity.sh [num_workers]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
RUN_ROOT="$REPO_ROOT/results/sensitivity_known_target_visit_grid_density_50"
MANIFEST="$RUN_ROOT/condition_manifest.csv"
LOG_DIR="$RUN_ROOT/worker_logs"

mkdir -p "$RUN_ROOT" "$LOG_DIR"
cd "$REPO_ROOT"

echo "============================================================"
echo "[SETUP] Known-target visit grid-density sensitivity"
echo "[INFO] repo root: $REPO_ROOT"
echo "[INFO] run root:  $RUN_ROOT"
echo "[INFO] workers:   $WORKERS"
echo "[INFO] targets/trial: 10"
echo "============================================================"

python3 "$SCRIPT_DIR/prepare_known_grid_density_sensitivity.py" \
  --repo-root "$REPO_ROOT" \
  --run-root "$RUN_ROOT" \
  --num-trials 50 \
  --num-targets 10

echo "[INFO] Launching partitioned workers..."
PIDS=()
for ((w=0; w<WORKERS; w++)); do
  python3 "$SCRIPT_DIR/run_known_grid_density_worker.py" \
    --repo-root "$REPO_ROOT" \
    --manifest "$MANIFEST" \
    --worker-index "$w" \
    --num-workers "$WORKERS" \
    > "$LOG_DIR/worker_${w}.log" 2>&1 &
  pid="$!"
  PIDS+=("$pid")
  echo "[INFO] worker $w pid=$pid log=$LOG_DIR/worker_${w}.log"
done

FAIL=0
for pid in "${PIDS[@]}"; do
  if ! wait "$pid"; then
    FAIL=1
  fi
done

if [[ "$FAIL" -ne 0 ]]; then
  echo "[ERROR] One or more workers failed. Check logs in $LOG_DIR" >&2
  exit 1
fi

echo "============================================================"
echo "[DONE] Raw known-target sensitivity runs complete."
echo "[NEXT] Combine with:"
echo "  bash known_visit_sim/tests/grid_density/combine_known_grid_density_sensitivity.sh"
echo "============================================================"
