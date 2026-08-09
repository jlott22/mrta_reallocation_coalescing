from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def campaign_status(campaign_root: Path) -> dict[str, Any]:
    state = json.loads(
        (campaign_root / "schedule.json").read_text(encoding="utf-8")
    )
    condition_counts: dict[str, int] = {}
    fixtures_total = 0
    fixtures_completed = 0
    for entry in state["conditions"].values():
        status = str(entry["status"])
        condition_counts[status] = condition_counts.get(status, 0) + 1
        fixtures_total += int(entry["fixture_total"])
        fixtures_completed += int(entry["fixtures_completed"])
    created = datetime.fromisoformat(state["created_at"])
    elapsed = max(
        0.0,
        (datetime.now(timezone.utc) - created).total_seconds(),
    )
    rate = fixtures_completed / elapsed if elapsed and fixtures_completed else 0.0
    eta = (
        (fixtures_total - fixtures_completed) / rate
        if rate > 0
        else None
    )
    return {
        "campaign_id": state["campaign_id"],
        "status": state["status"],
        "condition_counts": condition_counts,
        "fixtures_completed": fixtures_completed,
        "fixtures_total": fixtures_total,
        "progress_fraction": (
            fixtures_completed / fixtures_total if fixtures_total else 0.0
        ),
        "elapsed_seconds": elapsed,
        "eta_seconds": eta,
        "devices": list(state["devices"].values()),
        "active_conditions": [
            {
                "condition_id": entry["condition_id"],
                "status": entry["status"],
                "device_id": entry["pinned_device_id"],
                "fixtures_completed": entry["fixtures_completed"],
                "fixture_total": entry["fixture_total"],
                "classification": entry.get("classification", ""),
                "failure_fixture_id": entry.get("failure_fixture_id", ""),
            }
            for entry in state["conditions"].values()
            if entry["status"] in {"running", "paused", "stopped"}
        ],
    }
