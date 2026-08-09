from __future__ import annotations

from pathlib import Path
from typing import Any

from allocator_replay.hil.manifest import load_manifest


def hil_status(root: Path) -> dict[str, Any]:
    schedule = load_manifest(root)
    counts: dict[str, int] = {}
    completed_trials = 0
    failed_trials = 0
    planned_trials = 0
    for job in schedule["jobs"]:
        status = str(job["status"])
        counts[status] = counts.get(status, 0) + 1
        if job.get("excluded_from_core") or status == "excluded":
            continue
        completed_trials += len(job["completed_trials"])
        failed_trials += len(job.get("failed_trials", []))
        planned_trials += len(job["trial_ids"])
    active = sorted(
        schedule.get("active_trials", {}).values(),
        key=lambda item: (str(item.get("condition_id", "")), int(item.get("trial_id", 0))),
    )
    return {
        "campaign_id": schedule["campaign_id"],
        "status": schedule.get("status", "unknown"),
        "condition_counts": counts,
        "completed_trials": completed_trials,
        "failed_trials": failed_trials,
        "planned_trials": planned_trials,
        "resolved_trials": completed_trials + failed_trials,
        "progress": (
            (completed_trials + failed_trials) / planned_trials
            if planned_trials else 0
        ),
        "phase": schedule.get("phase", ""),
        "active": active,
        "devices": schedule.get("devices", {}),
        "updated_at": schedule.get("updated_at", ""),
    }
