"""Live tracker for the final minimal three-board experiment."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT / "study/output/final_minimal_unattended_v2"
STATE = RUN_ROOT / "state.json"
TRACKER = RUN_ROOT / "LIVE_TRACKER.md"
STAGES = [
    ("hardware_smoke", 16, ROOT / "study/output/agx_causal_smoke_v2/causal/completed"),
    ("arrival_verification", 72, ROOT / "study/output/agx_minimal_arrival_verify_v1/completed"),
    ("timeout_verification", 72, ROOT / "study/output/agx_minimal_timeout_verify_v1/completed"),
    ("agx_full_n25", 1500, ROOT / "study/output/agx_minimal_full_n25_v1/completed"),
    ("zero_compute", 1500, ROOT / "study/output/agx_minimal_zero_compute_n25_v1/completed"),
    ("hardware_core", 96, ROOT / "study/output/agx_minimal_hardware_core_96_v1/causal/completed"),
    ("hardware_bounded", 24, ROOT / "study/output/agx_minimal_hardware_bounded_24_v1/causal/completed"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def count(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for path in root.iterdir() if path.is_dir())


def read_state() -> dict:
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def render() -> None:
    state = read_state()
    phase = str(state.get("phase", "initializing"))
    status = str(state.get("status", "running"))
    started = float(state.get("stage_started_epoch", time.time()))
    elapsed = max(0.0, time.time() - started)
    current = next(
        ((name, total, root) for name, total, root in STAGES if name == phase),
        None,
    )
    eta = "measuring"
    if current:
        complete = count(current[2])
        if 0 < complete < current[1]:
            eta_s = elapsed * (current[1] - complete) / complete
            eta = f"{eta_s / 3600.0:.1f} h"
        elif complete >= current[1]:
            eta = "0 h"
    rows = []
    completed_total = 0
    planned_total = 0
    for name, total, root in STAGES:
        complete = count(root)
        completed_total += min(complete, total)
        planned_total += total
        rows.append(f"| {name} | {complete} | {total} |")
    content = "\n".join(
        [
            "# Final minimal experiment live tracker",
            "",
            f"Updated: {now()}",
            f"Status: **{status}**",
            f"Current phase: **{phase}**",
            f"Current phase provisional ETA: **{eta}**",
            f"Overall completed missions: **{completed_total}/{planned_total}**",
            f"Runner PID: {state.get('runner_pid', '')}",
            f"Git commit: {state.get('git_commit', '')}",
            "",
            "| Stage | Completed | Planned |",
            "|---|---:|---:|",
            *rows,
            "",
            "Three-board user-approved deviation: hardware publication work uses three boards;",
            "AGX statistical stages use exactly four workers. Motors and sensors are not initialized.",
            "The immutable v1 smoke output is preserved separately and is not counted in v2.",
            "",
            f"Last message: {state.get('message', '')}",
            "",
        ]
    )
    atomic(TRACKER, content)


def set_state(args: argparse.Namespace) -> None:
    previous = read_state()
    state = {
        **previous,
        "phase": args.phase,
        "status": args.status,
        "message": args.message,
        "stage_started_epoch": time.time(),
        "updated_at": now(),
        "runner_pid": args.runner_pid or previous.get("runner_pid"),
        "git_commit": args.git_commit or previous.get("git_commit"),
    }
    atomic(STATE, json.dumps(state, indent=2, sort_keys=True) + "\n")
    render()


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    setter = sub.add_parser("set")
    setter.add_argument("--phase", required=True)
    setter.add_argument("--status", default="running")
    setter.add_argument("--message", default="")
    setter.add_argument("--runner-pid", type=int)
    setter.add_argument("--git-commit")
    watcher = sub.add_parser("watch")
    watcher.add_argument("--interval", type=float, default=30.0)
    args = parser.parse_args()
    if args.command == "set":
        set_state(args)
        return 0
    while True:
        render()
        if read_state().get("status") in {"complete", "failed"}:
            return 0
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
