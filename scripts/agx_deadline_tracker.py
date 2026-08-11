#!/usr/bin/env python3
"""Concurrent live tracker for the August 14 deadline campaign."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT / "study/output/agx_deadline_aug14_v1"
STATE = RUN_ROOT / "state.json"
STATE_LOCK = RUN_ROOT / "state.lock"
TRACKER = RUN_ROOT / "LIVE_TRACKER.md"
STAGES = [
    (
        "hardware_core",
        "hardware",
        96,
        ROOT / "study/output/agx_deadline_hardware_core_96_v1/causal/completed",
    ),
    (
        "hardware_bounded",
        "hardware",
        8,
        ROOT / "study/output/agx_deadline_hardware_bounded_8_v1/causal/completed",
    ),
    (
        "agx_primary",
        "agx",
        1396,
        ROOT / "study/output/agx_deadline_primary_1396_v1/completed",
    ),
    (
        "arrival_verification",
        "agx",
        24,
        ROOT / "study/output/agx_deadline_arrival_verify_24_v1/completed",
    ),
    (
        "timeout_verification",
        "agx",
        24,
        ROOT / "study/output/agx_deadline_timeout_verify_24_v1/completed",
    ),
    (
        "zero_compute",
        "agx",
        480,
        ROOT / "study/output/agx_deadline_zero_compute_480_v1/completed",
    ),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def state_guard() -> Iterator[None]:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    with STATE_LOCK.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def read_state() -> dict:
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def count(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(path.is_dir() for path in root.iterdir())


def failure_count() -> int:
    roots = (
        ROOT / "study/output/agx_deadline_hardware_core_96_v1/causal/attempts",
        ROOT / "study/output/agx_deadline_hardware_bounded_8_v1/causal/attempts",
        ROOT / "study/output/agx_deadline_primary_1396_v1/attempts",
        ROOT / "study/output/agx_deadline_arrival_verify_24_v1/attempts",
        ROOT / "study/output/agx_deadline_timeout_verify_24_v1/attempts",
        ROOT / "study/output/agx_deadline_zero_compute_480_v1/attempts",
    )
    return sum(
        1
        for root in roots
        if root.is_dir()
        for _ in root.rglob("failure.json")
    )


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "measuring"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}h {minutes:02d}m"


def pipeline_snapshot(state: dict, pipeline: str) -> dict:
    stages = [stage for stage in STAGES if stage[1] == pipeline]
    completed = sum(min(count(root), planned) for _, _, planned, root in stages)
    planned = sum(stage[2] for stage in stages)
    pipeline_state = state.get("pipelines", {}).get(pipeline, {})
    started = pipeline_state.get("started_epoch")
    eta_seconds = None
    throughput = None
    if isinstance(started, (int, float)) and completed > 0 and completed < planned:
        elapsed = max(0.0, time.time() - float(started))
        throughput = completed / elapsed if elapsed > 0 else None
        eta_seconds = (
            (planned - completed) / throughput
            if throughput is not None and throughput > 0
            else None
        )
    elif completed >= planned:
        eta_seconds = 0.0
    return {
        "completed": completed,
        "planned": planned,
        "status": pipeline_state.get("status", "pending"),
        "stage": pipeline_state.get("stage", "pending"),
        "message": pipeline_state.get("message", ""),
        "eta_seconds": eta_seconds,
        "throughput_per_hour": None if throughput is None else throughput * 3600.0,
    }


def render() -> None:
    state = read_state()
    hardware = pipeline_snapshot(state, "hardware")
    agx = pipeline_snapshot(state, "agx")
    pipeline_values = (hardware, agx)
    eta_values = [
        value["eta_seconds"]
        for value in pipeline_values
        if value["eta_seconds"] is not None
    ]
    overall_eta = (
        max(eta_values)
        if eta_values
        and all(
            value["eta_seconds"] is not None
            or value["completed"] >= value["planned"]
            for value in pipeline_values
        )
        else None
    )
    finish = (
        datetime.now(timezone.utc) + timedelta(seconds=overall_eta)
        if overall_eta is not None
        else None
    )
    rows = []
    completed_total = 0
    planned_total = 0
    for name, pipeline, planned, root in STAGES:
        completed = count(root)
        completed_total += min(completed, planned)
        planned_total += planned
        rows.append(f"| {name} | {pipeline} | {completed} | {planned} |")
    throughput_text = lambda value: (
        "measuring"
        if value["throughput_per_hour"] is None
        else f"{value['throughput_per_hour']:.2f} missions/hour"
    )
    lines = [
        "# August 14 deadline campaign live tracker",
        "",
        f"Updated: {now()}",
        f"Overall status: **{state.get('overall_status', 'initializing')}**",
        f"Overall progress: **{completed_total}/{planned_total}**",
        f"Provisional critical-path ETA: **{format_duration(overall_eta)}**",
        "Estimated finish (UTC): **{}**".format(
            "measuring" if finish is None else finish.isoformat().replace("+00:00", "Z")
        ),
        f"Retained technical failure records: **{failure_count()}**",
        f"Runner PID: {state.get('runner_pid', '')}",
        f"Git commit: {state.get('git_commit', '')}",
        "",
        "## Concurrent pipelines",
        "",
        "| Pipeline | Status | Current stage | Progress | Throughput | ETA |",
        "|---|---|---|---:|---:|---:|",
        (
            f"| Hardware, CPUs 0-2 | {hardware['status']} | {hardware['stage']} | "
            f"{hardware['completed']}/{hardware['planned']} | "
            f"{throughput_text(hardware)} | {format_duration(hardware['eta_seconds'])} |"
        ),
        (
            f"| AGX, CPUs 3-8 | {agx['status']} | {agx['stage']} | "
            f"{agx['completed']}/{agx['planned']} | "
            f"{throughput_text(agx)} | {format_duration(agx['eta_seconds'])} |"
        ),
        "",
        "## Matrix",
        "",
        "| Stage | Pipeline | Completed | Planned |",
        "|---|---|---:|---:|",
        *rows,
        "",
        "CPU contract: three pinned RP2040 workers on CPUs 0-2; six rolling,",
        "single-threaded AGX workers confined to CPUs 3-8; CPUs 9-11 reserved.",
        "The five completed v2 smoke missions remain diagnostic-only and are excluded.",
        "",
        f"Hardware message: {hardware['message']}",
        f"AGX message: {agx['message']}",
        f"Overall message: {state.get('message', '')}",
        "",
    ]
    atomic(TRACKER, "\n".join(lines))


def update_state(args: argparse.Namespace) -> None:
    with state_guard():
        state = read_state()
        if args.pipeline == "overall":
            state["overall_status"] = args.status
            state["message"] = args.message
            if args.runner_pid is not None:
                state["runner_pid"] = args.runner_pid
            if args.git_commit:
                state["git_commit"] = args.git_commit
        else:
            pipelines = state.setdefault("pipelines", {})
            previous = pipelines.get(args.pipeline, {})
            started = previous.get("started_epoch", time.time())
            if previous.get("stage") != args.stage:
                stage_started = time.time()
            else:
                stage_started = previous.get("stage_started_epoch", time.time())
            pipelines[args.pipeline] = {
                **previous,
                "status": args.status,
                "stage": args.stage,
                "message": args.message,
                "started_epoch": started,
                "stage_started_epoch": stage_started,
                "updated_at": now(),
            }
        state["updated_at"] = now()
        atomic(STATE, json.dumps(state, indent=2, sort_keys=True) + "\n")
    render()


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    setter = sub.add_parser("set")
    setter.add_argument("--pipeline", choices=("overall", "agx", "hardware"), required=True)
    setter.add_argument("--stage", default="")
    setter.add_argument("--status", default="running")
    setter.add_argument("--message", default="")
    setter.add_argument("--runner-pid", type=int)
    setter.add_argument("--git-commit")
    watcher = sub.add_parser("watch")
    watcher.add_argument("--interval", type=float, default=30.0)
    args = parser.parse_args()
    if args.command == "set":
        update_state(args)
        return 0
    while True:
        render()
        if read_state().get("overall_status") in {"complete", "failed"}:
            return 0
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
