#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
RUN_ROOT="$REPO_ROOT/results/sensitivity_known_target_visit_grid_density_50"

cd "$REPO_ROOT"
python3 "$SCRIPT_DIR/combine_known_grid_density_sensitivity.py" --run-root "$RUN_ROOT"

echo ""
echo "[DONE] Combined CSVs are in:"
echo "$RUN_ROOT/combined"
echo ""
echo "Optional verification:"
echo "python3 known_visit_sim/tests/grid_density/verify_known_grid_density_sensitivity.py --run-root $RUN_ROOT"
