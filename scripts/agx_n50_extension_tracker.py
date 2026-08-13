#!/usr/bin/env python3
"""Dedicated live tracker for the n=50 AGX-only extension."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "study/output/agx_n50_extension_v1"
STATE = ROOT / "state.json"
TRACKER = ROOT / "LIVE_TRACKER.md"
STAGES = (
    (
        "agx_causal_new_traces",
        1500,
        REPO / "study/output/agx_n50_extension_causal_1500_v1/completed",
        REPO / "study/output/agx_n50_extension_causal_1500_v1/attempts",
    ),
    (
        "zero_compute_new_traces",
        1500,
        REPO / "study/output/agx_n50_extension_zero_compute_1500_v1/completed",
        REPO / "study/output/agx_n50_extension_zero_compute_1500_v1/attempts",
    ),
)


def count_dirs(path: Path) -> int:
    return sum(item.is_dir() for item in path.iterdir()) if path.is_dir() else 0


def failure_count(path: Path) -> int:
    return sum(1 for _ in path.rglob("failure.json")) if path.is_dir() else 0


def read_state() -> dict:
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def render() -> None:
    state = read_state()
    completed = sum(min(count_dirs(root), planned) for _, planned, root, _ in STAGES)
    planned = sum(item[1] for item in STAGES)
    failures = sum(failure_count(attempts) for _, _, _, attempts in STAGES)
    started = state.get("started_epoch")
    rate = None
    if isinstance(started, (int, float)) and completed > 0:
        rate = completed / max(1.0, time.time() - float(started))
    remaining_s = None if not rate else max(0, planned - completed) / rate
    finish = None if remaining_s is None else datetime.now(timezone.utc) + timedelta(seconds=remaining_s)
    if remaining_s is None:
        eta = "measuring"
    else:
        hours, remainder = divmod(int(remaining_s), 3600)
        eta = f"{hours}h {remainder // 60:02d}m"
    rows = [
        f"| {name} | {count_dirs(root)} | {planned_count} | {failure_count(attempts)} |"
        for name, planned_count, root, attempts in STAGES
    ]
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    lines = [
        "# AGX n=50 extension live tracker",
        "",
        f"Updated: {now}",
        f"Status: **{state.get('status', 'initializing')}**",
        f"Current stage: **{state.get('stage', 'initializing')}**",
        f"Progress: **{completed}/{planned}**",
        f"Retained attempt failures: **{failures}**",
        f"Estimated remaining time: **{eta}**",
        "Estimated finish (UTC): **{}**".format(
            "measuring" if finish is None else finish.isoformat().replace("+00:00", "Z")
        ),
        f"Supervisor PID: {state.get('supervisor_pid', '')}",
        f"Source commit: {state.get('git_commit', '')}",
        f"Message: {state.get('message', '')}",
        "",
        "| Stage | Completed | Planned | Failure records |",
        "|---|---:|---:|---:|",
        *rows,
        "",
        "This extension uses only new trace IDs 0025-0049 on CPUs 3-8.",
        "Its outputs, logs, state, lock, and tracker are separate from all prior campaigns.",
        "Hardware remains isolated on CPUs 0-2.",
        "",
    ]
    atomic(TRACKER, "\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    while True:
        render()
        if args.once:
            return 0
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
