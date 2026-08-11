#!/usr/bin/env python3
"""Write the terminal audit report for the concurrent deadline campaign."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT / "study/output/agx_deadline_aug14_v1"
OUTPUT = RUN_ROOT / "FINAL_EXPERIMENT_REPORT.md"
STAGES = [
    ("Hardware core Eager/B4", 96, ROOT / "study/output/agx_deadline_hardware_core_96_v1/causal/completed"),
    ("Hardware bounded B4/W5", 8, ROOT / "study/output/agx_deadline_hardware_bounded_8_v1/causal/completed"),
    ("AGX-only primary remainder", 1396, ROOT / "study/output/agx_deadline_primary_1396_v1/completed"),
    ("AGX arrival verification", 24, ROOT / "study/output/agx_deadline_arrival_verify_24_v1/completed"),
    ("AGX timeout verification", 24, ROOT / "study/output/agx_deadline_timeout_verify_24_v1/completed"),
    ("AGX zero-compute controls", 480, ROOT / "study/output/agx_deadline_zero_compute_480_v1/completed"),
]


def count(root: Path) -> int:
    return sum(path.is_dir() for path in root.iterdir()) if root.is_dir() else 0


rows = [(name, count(root), planned) for name, planned, root in STAGES]
passed = all(done == planned for _, done, planned in rows)
smoke_root = ROOT / "study/output/agx_causal_smoke_v2"
smoke_completed = count(smoke_root / "causal/completed")
smoke_failures = (
    len(list((smoke_root / "causal/attempts").glob("*/attempt_*/failure.json")))
    if (smoke_root / "causal/attempts").is_dir()
    else 0
)
lines = [
    "# August 14 deadline experiment execution report",
    "",
    f"Generated: {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}",
    f"Overall execution status: **{'PASS' if passed else 'INCOMPLETE'}**",
    "",
    "| Stage | Completed | Planned |",
    "|---|---:|---:|",
    *[f"| {name} | {done} | {planned} |" for name, done, planned in rows],
    "",
    "Primary design accounting:",
    "- AGX-only primary missions: 1,396",
    "- Hardware-backed primary missions: 104",
    "- Total primary missions: 1,500",
    "- Balanced zero-compute controls: 480 (eight per condition)",
    "- Separate one-trace verification missions: 48",
    "",
    "Execution contract:",
    "- Pololu workers pinned to CPUs 0-2.",
    "- Six rolling AGX workers confined to CPUs 3-8.",
    "- CPUs 9-11 reserved; hidden library threads fixed at one.",
    "- Standalone smoke waived by explicit user direction; first primary hardware trace was the retained canary.",
    "",
    "Superseded v2 smoke evidence:",
    f"- Completed diagnostic missions preserved: {smoke_completed}/16",
    f"- Retained technical failure records: {smoke_failures}",
    "- These missions are excluded from primary and publication counts.",
    "",
    "Analysis outputs:",
    "- study/output/agx_deadline_primary_1396_v1/analysis/",
    "- study/output/agx_deadline_arrival_verify_24_v1/analysis/",
    "- study/output/agx_deadline_timeout_verify_24_v1/analysis/",
    "- study/output/agx_deadline_zero_compute_480_v1/analysis/",
    "- study/output/agx_deadline_hardware_core_96_v1/analysis/",
    "- study/output/agx_deadline_hardware_bounded_8_v1/analysis/",
    "",
]
RUN_ROOT.mkdir(parents=True, exist_ok=True)
temporary = OUTPUT.with_name(f".{OUTPUT.name}.{os.getpid()}.tmp")
temporary.write_text("\n".join(lines), encoding="utf-8")
os.replace(temporary, OUTPUT)
raise SystemExit(0 if passed else 2)
