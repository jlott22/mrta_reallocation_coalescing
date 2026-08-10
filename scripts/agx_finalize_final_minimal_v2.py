"""Write the terminal report for the final minimal three-board experiment."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT / "study/output/final_minimal_unattended_v2"
OUTPUT = RUN_ROOT / "FINAL_EXPERIMENT_REPORT.md"
STAGES = [
    ("Hardware smoke v2", 16, ROOT / "study/output/agx_causal_smoke_v2/causal/completed"),
    ("AGX arrival verification", 72, ROOT / "study/output/agx_minimal_arrival_verify_v1/completed"),
    ("AGX timeout verification", 72, ROOT / "study/output/agx_minimal_timeout_verify_v1/completed"),
    ("AGX full n=25", 1500, ROOT / "study/output/agx_minimal_full_n25_v1/completed"),
    ("Zero compute", 1500, ROOT / "study/output/agx_minimal_zero_compute_n25_v1/completed"),
    ("Hardware core", 96, ROOT / "study/output/agx_minimal_hardware_core_96_v1/causal/completed"),
    ("Hardware bounded", 24, ROOT / "study/output/agx_minimal_hardware_bounded_24_v1/causal/completed"),
]


def count(root: Path) -> int:
    return sum(1 for path in root.iterdir() if path.is_dir()) if root.is_dir() else 0


rows = [(name, count(root), planned) for name, planned, root in STAGES]
passed = all(done == planned for _, done, planned in rows)
prior_root = ROOT / "study/output/agx_causal_smoke_v1"
prior_completed = count(prior_root / "causal/completed")
prior_failures = (
    len(list((prior_root / "causal/attempts").glob("*/attempt_*/failure.json")))
    if (prior_root / "causal/attempts").is_dir()
    else 0
)
lines = [
    "# Final minimal experiment execution report",
    "",
    f"Generated: {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}",
    f"Overall execution status: **{'PASS' if passed else 'INCOMPLETE'}**",
    "",
    "| Stage | Completed | Planned |",
    "|---|---:|---:|",
    *[f"| {name} | {done} | {planned} |" for name, done, planned in rows],
    "",
    "Protocol deviation: the user explicitly directed execution on the three detected",
    "Pololu/RP2040 boards instead of the four-board concurrency stated in the uploaded",
    "minimal plan. The 120 publication conditions and within-block pairing are unchanged.",
    "",
    "Superseded engineering smoke evidence:",
    f"- v1 promoted missions preserved: {prior_completed}/16",
    f"- v1 retained technical failure records: {prior_failures}",
    "- v1 is diagnostic only; corrected source was not resumed into its immutable schedule.",
    "",
    "AGX analysis outputs:",
    "- study/output/agx_minimal_arrival_verify_v1/analysis/",
    "- study/output/agx_minimal_timeout_verify_v1/analysis/",
    "- study/output/agx_minimal_full_n25_v1/analysis/",
    "- study/output/agx_minimal_zero_compute_n25_v1/analysis/",
    "",
    "Hardware descriptive analysis outputs:",
    "- study/output/agx_minimal_hardware_core_96_v1/analysis/",
    "- study/output/agx_minimal_hardware_bounded_24_v1/analysis/",
    "",
]
RUN_ROOT.mkdir(parents=True, exist_ok=True)
temporary = OUTPUT.with_name(f".{OUTPUT.name}.{os.getpid()}.tmp")
temporary.write_text("\n".join(lines), encoding="utf-8")
os.replace(temporary, OUTPUT)
raise SystemExit(0 if passed else 2)
