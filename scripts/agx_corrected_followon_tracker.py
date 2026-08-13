#!/usr/bin/env python3
"""Track corrected AGX-only work and surface it in the deadline tracker."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CONTROL = Path("/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_aug14_v1")
OUTPUT = CONTROL / "CORRECTED_AGX_TRACKER.md"
STATE = CONTROL / "corrected_agx_state.json"
MAIN_TRACKER = Path("/home/agxorin/mrta_reallocation_coalescing/scripts/agx_deadline_tracker.py")
STAGES = (
    ("corrected_zero_480", 480, REPO / "study/output/agx_deadline_zero_compute_480_scheduler_v2/completed"),
    ("arrival_verify_additional", 48, REPO / "study/output/agx_deadline_arrival_verify_traces_1_2_v2/completed"),
    ("timeout_verify_additional", 48, REPO / "study/output/agx_deadline_timeout_verify_traces_1_2_v2/completed"),
    ("full_zero_extension", 1020, REPO / "study/output/agx_deadline_zero_compute_traces_8_24_v2/completed"),
)


def count(path: Path) -> int:
    return sum(item.is_dir() for item in path.iterdir()) if path.is_dir() else 0


def atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def snapshot() -> tuple[str, int, int, float | None, str]:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    completed = sum(min(count(path), planned) for _, planned, path in STAGES)
    planned = sum(item[1] for item in STAGES)
    started = state.get("started_epoch")
    rate = None
    if isinstance(started, (int, float)) and completed:
        elapsed = max(1.0, time.time() - float(started))
        rate = completed / elapsed
    remaining_s = None if rate is None else (planned - completed) / rate
    return str(state.get("stage", "initializing")), completed, planned, remaining_s, str(state.get("message", ""))


def render() -> None:
    stage, completed, planned, remaining_s, message = snapshot()
    now = datetime.now(timezone.utc)
    eta = "measuring"
    finish = "measuring"
    if remaining_s is not None:
        hours, remainder = divmod(max(0, int(remaining_s)), 3600)
        minutes = remainder // 60
        eta = f"{hours}h {minutes:02d}m"
        finish = (now + timedelta(seconds=remaining_s)).isoformat().replace("+00:00", "Z")
    rows = [
        f"| {name} | {count(path)} | {planned_count} |"
        for name, planned_count, path in STAGES
    ]
    lines = [
        "# Corrected AGX-only follow-on tracker",
        "",
        f"Updated: {now.isoformat().replace('+00:00', 'Z')}",
        f"Stage: **{stage}**",
        f"Progress: **{completed}/{planned}**",
        f"Estimated AGX finish: **{eta}** ({finish})",
        f"Message: {message}",
        "",
        "| Stage | Completed | Planned |",
        "|---|---:|---:|",
        *rows,
        "",
        "All work is confined to CPUs 3-8. Hardware remains on CPUs 0-2.",
        "",
    ]
    atomic(OUTPUT, "\n".join(lines))
    summary = f"{stage}: {completed}/{planned} corrected/follow-on AGX trials; ETA {eta}; details {OUTPUT}"
    subprocess.run(
        [
            "python3", str(MAIN_TRACKER), "set", "--pipeline", "agx",
            "--stage", stage, "--status", "running", "--message", summary,
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=30.0)
    args = parser.parse_args()
    while True:
        render()
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
