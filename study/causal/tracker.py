"""Continuously render causal campaign progress and a provisional wall-time ETA."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            # A concurrent append may expose one incomplete final line.
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "estimating"
    seconds = max(0, round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes}m {seconds:02d}s"


def collect_snapshot(
    campaign_root: Path,
    *,
    zero_compute: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read a coherent-enough progress snapshot without mutating campaign data."""

    current_time = now or _utc_now()
    result_kind = "zero_compute" if zero_compute else "causal"
    schedule_name = "zero_compute_schedule.json" if zero_compute else "causal_schedule.json"
    events_name = "zero_compute_events.jsonl" if zero_compute else "campaign_events.jsonl"
    schedule = _read_json(campaign_root / schedule_name)
    blocks = schedule.get("blocks", []) if schedule else []
    jobs = [
        job
        for block in blocks
        if isinstance(block, dict)
        for job in block.get("jobs", [])
        if isinstance(job, dict) and isinstance(job.get("job_id"), str)
    ]
    planned_ids = {str(job["job_id"]) for job in jobs}
    completed_root = campaign_root / result_kind / "completed"
    completed_ids = {
        path.name
        for path in completed_root.iterdir()
        if completed_root.is_dir() and path.is_dir() and path.name in planned_ids
    } if completed_root.is_dir() else set()

    events = _read_events(campaign_root / events_name)
    invocation_id = next(
        (
            str(event["invocation_id"])
            for event in reversed(events)
            if isinstance(event.get("invocation_id"), str)
        ),
        None,
    )
    invocation_events = [
        event for event in events if event.get("invocation_id") == invocation_id
    ] if invocation_id else []
    event_times = [
        parsed
        for event in invocation_events
        if (parsed := _parse_timestamp(event.get("recorded_at"))) is not None
    ]
    started_at = min(event_times) if event_times else None
    elapsed_s = (
        max(0.0, (current_time - started_at).total_seconds())
        if started_at is not None else None
    )
    new_completions = {
        str(event["job_id"])
        for event in invocation_events
        if event.get("type") == "job"
        and event.get("status") == "completed"
        and isinstance(event.get("job_id"), str)
    }
    remaining = max(0, len(planned_ids) - len(completed_ids))
    throughput = (
        len(new_completions) / elapsed_s
        if elapsed_s and new_completions else None
    )
    eta_s = 0.0 if planned_ids and remaining == 0 else (
        remaining / throughput if throughput and throughput > 0.0 else None
    )
    estimated_finish = current_time + timedelta(seconds=eta_s) if eta_s is not None else None

    ready_workers = {
        int(event["worker_index"])
        for event in invocation_events
        if event.get("type") == "worker_ready" and isinstance(event.get("worker_index"), int)
    }
    done_workers = {
        int(event["worker_index"])
        for event in invocation_events
        if event.get("type") == "worker_done" and isinstance(event.get("worker_index"), int)
    }
    fatal_events = [event for event in invocation_events if event.get("type") == "worker_fatal"]
    if planned_ids and remaining == 0:
        status = "COMPLETE"
    elif fatal_events or (ready_workers and ready_workers == done_workers and remaining):
        status = "INCOMPLETE"
    elif ready_workers:
        status = "RUNNING"
    else:
        status = "WAITING"

    planned_by_worker: Counter[int] = Counter()
    completed_by_worker: Counter[int] = Counter()
    for job in jobs:
        worker_index = int(job.get("worker_index", -1))
        planned_by_worker[worker_index] += 1
        if job["job_id"] in completed_ids:
            completed_by_worker[worker_index] += 1
    last_event_by_worker: dict[int, str] = {}
    for event in invocation_events:
        worker_index = event.get("worker_index")
        if isinstance(worker_index, int):
            last_event_by_worker[worker_index] = str(
                event.get("status", event.get("type", "unknown"))
            )

    return {
        "updated_at": current_time,
        "status": status,
        "terminal": status in {"COMPLETE", "INCOMPLETE"},
        "result_kind": result_kind,
        "invocation_id": invocation_id,
        "planned": len(planned_ids),
        "completed": len(completed_ids),
        "remaining": remaining,
        "progress_percent": (
            100.0 * len(completed_ids) / len(planned_ids) if planned_ids else 0.0
        ),
        "elapsed_s": elapsed_s,
        "throughput_jobs_per_s": throughput,
        "eta_s": eta_s,
        "estimated_finish": estimated_finish,
        "technical_attempt_failures": sum(
            event.get("type") == "attempt_failure" for event in invocation_events
        ),
        "fatal_worker_events": len(fatal_events),
        "workers": [
            {
                "worker_index": worker_index,
                "planned": planned_by_worker[worker_index],
                "completed": completed_by_worker[worker_index],
                "remaining": planned_by_worker[worker_index] - completed_by_worker[worker_index],
                "last_event": last_event_by_worker.get(worker_index, "waiting"),
            }
            for worker_index in sorted(planned_by_worker)
        ],
        "recent_events": invocation_events[-8:],
    }


def render_markdown(snapshot: Mapping[str, Any], *, refresh_seconds: float) -> str:
    updated_at = snapshot["updated_at"].isoformat().replace("+00:00", "Z")
    finish = snapshot["estimated_finish"]
    finish_text = finish.isoformat().replace("+00:00", "Z") if finish else "estimating"
    throughput = snapshot["throughput_jobs_per_s"]
    jobs_per_hour = throughput * 3600.0 if throughput is not None else None
    rows = [
        "# Live causal campaign tracker",
        "",
        f"_Rewritten every {refresh_seconds:g} seconds; last refresh {updated_at}._",
        "",
        f"Status: **{snapshot['status']}**",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Progress | {snapshot['completed']} / {snapshot['planned']} ({snapshot['progress_percent']:.1f}%) |",
        f"| Remaining jobs | {snapshot['remaining']} |",
        f"| Elapsed | {_duration(snapshot['elapsed_s'])} |",
        f"| ETA | {_duration(snapshot['eta_s'])} |",
        f"| Estimated finish (UTC) | {finish_text} |",
        f"| Measured throughput | {jobs_per_hour:.2f} jobs/hour |" if jobs_per_hour is not None else "| Measured throughput | estimating |",
        f"| Technical attempt failures | {snapshot['technical_attempt_failures']} |",
        f"| Fatal worker events | {snapshot['fatal_worker_events']} |",
        "",
        "> ETA is provisional and is recalculated from jobs completed during the latest invocation.",
        "",
        "## Workers",
        "",
        "| Worker | Completed | Planned | Remaining | Last event |",
        "|---:|---:|---:|---:|---|",
    ]
    for worker in snapshot["workers"]:
        rows.append(
            f"| {worker['worker_index']} | {worker['completed']} | {worker['planned']} | "
            f"{worker['remaining']} | {worker['last_event']} |"
        )
    rows.extend(["", "## Recent events", ""])
    if snapshot["recent_events"]:
        rows.extend(["| Recorded at | Worker | Event | Job |", "|---|---:|---|---|"])
        for event in reversed(snapshot["recent_events"]):
            rows.append(
                f"| {event.get('recorded_at', '')} | {event.get('worker_index', '')} | "
                f"{event.get('status', event.get('type', ''))} | {event.get('job_id', '')} |"
            )
    else:
        rows.append("No campaign events recorded yet.")
    return "\n".join(rows) + "\n"


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _campaign_root(config_path: Path, repo_root: Path) -> Path:
    config = _read_json(config_path)
    if config is None or not isinstance(config.get("campaign"), dict):
        raise ValueError(f"invalid causal campaign config: {config_path}")
    raw = config["campaign"].get("output_root")
    if not isinstance(raw, str) or not raw:
        raise ValueError("campaign.output_root must be a nonempty string")
    root = (repo_root / raw).resolve()
    if root == repo_root or repo_root not in root.parents:
        raise ValueError("campaign.output_root escapes the repository")
    return root


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--zero-compute", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--refresh-seconds", type=float, default=5.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.refresh_seconds <= 0.0:
        raise ValueError("refresh interval must be positive")
    repo_root = args.repo_root.resolve()
    config_path = args.config if args.config.is_absolute() else repo_root / args.config
    campaign_root = _campaign_root(config_path.resolve(), repo_root)
    output = args.output or (campaign_root / "LIVE_TRACKER.md")
    if not output.is_absolute():
        output = (repo_root / output).resolve()
    while True:
        snapshot = collect_snapshot(campaign_root, zero_compute=args.zero_compute)
        _atomic_text(output, render_markdown(snapshot, refresh_seconds=args.refresh_seconds))
        print(
            f"{snapshot['status']} {snapshot['completed']}/{snapshot['planned']} "
            f"ETA={_duration(snapshot['eta_s'])} tracker={output}",
            flush=True,
        )
        if not args.watch or snapshot["terminal"]:
            return 0 if snapshot["status"] != "INCOMPLETE" else 1
        time.sleep(args.refresh_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
