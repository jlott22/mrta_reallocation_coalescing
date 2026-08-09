from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from allocator_replay.config.study import REPOSITORY_ROOT
from allocator_replay.hil.manifest import (
    implementation_provenance,
    load_manifest,
    save_schedule,
    sha256_file,
)
from allocator_replay.hil.watchdog import (
    EVENT_CAP,
    NO_MOVEMENT_EVENTS,
    NO_PROGRESS_EVENTS,
    PROGRESS_CONFIRMATION_EVENTS,
    REPEATED_STATE_COUNT,
    REPEATED_STATE_WINDOW,
    SHORT_CONFIRMATION_EVENTS,
)


ARCHIVE_NAME = "bayesian_k1_removed_20260801"
EXCLUSION_REASON = "user_removed_bayesian_k1_20260801"


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _work_estimates(algorithm: str, top_k_level: str) -> dict[str, float]:
    rate = int(round(float(top_k_level.rstrip("%"))))
    path = (
        REPOSITORY_ROOT
        / "Results"
        / "Simulation"
        / "TopKLowKSupplement"
        / "AllMissionsFullCounts"
        / "raw"
        / "bayesian"
        / algorithm.lower()
        / f"topk_{rate:03d}"
        / "system_performance.csv"
    )
    if not path.exists():
        return {}
    result: dict[str, float] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            calls = row.get("allocator_calls_total")
            if calls:
                result[str(int(row["trial_id"]))] = float(calls)
    return result


def _archive_records(
    root: Path,
    archive: Path,
    condition_ids: set[str],
) -> tuple[Path, int]:
    destination = archive / "bayesian_k1_journal_records.jsonl"
    temporary = destination.with_suffix(".jsonl.tmp")
    count = 0
    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        for journal in sorted((root / "journals").glob("*.jsonl")):
            with journal.open(encoding="utf-8") as source:
                for line in source:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if row.get("condition_id") in condition_ids:
                        output.write(line.rstrip("\r\n") + "\n")
                        count += 1
    os.replace(temporary, destination)
    return destination, count


def apply_cutover(root: Path) -> dict[str, Any]:
    root = root.resolve()
    schedule = load_manifest(root)
    manifest = load_manifest(root, "manifest.json")
    archive = root / "archive" / ARCHIVE_NAME
    archive.mkdir(parents=True, exist_ok=True)
    archive_marker = archive / "manifest.json"
    old_manifest_path = archive / "manifest_before_cutover.json"
    if not old_manifest_path.exists():
        shutil.copy2(root / "manifest.json", old_manifest_path)

    k1_jobs = [
        job
        for job in schedule["jobs"]
        if job["mission"] == "bayesian" and int(job["top_k_cells"]) == 1
    ]
    condition_ids = {str(job["condition_id"]) for job in k1_jobs}
    pre_cutover = sorted(root.glob("schedule.before_watchdog_cutover.*.json"))
    schedule_source = pre_cutover[-1] if pre_cutover else root / "schedule.json"
    schedule_archive = archive / "schedule_before_cutover.json"
    if not schedule_archive.exists():
        shutil.copy2(schedule_source, schedule_archive)

    records_path = archive / "bayesian_k1_journal_records.jsonl"
    if not records_path.exists():
        records_path, record_count = _archive_records(
            root, archive, condition_ids
        )
    else:
        with records_path.open(encoding="utf-8") as handle:
            record_count = sum(1 for line in handle if line.strip())

    for job in k1_jobs:
        job["status"] = "excluded"
        job["excluded_from_core"] = True
        job["excluded_reason"] = EXCLUSION_REASON
        job["device_id"] = ""

    retained = {
        "bayesian_pi_topk_003_k11",
        "bayesian_hipc_topk_001_k4",
    }
    bayesian_terminal = all(
        job["status"] in {
            "complete",
            "completed_with_trial_failures",
            "stopped",
            "excluded",
        }
        for job in schedule["jobs"]
        if job["mission"] == "bayesian"
    )
    for job in schedule["jobs"]:
        if job["condition_id"] in retained:
            if job["status"] == "running":
                job["status"] = "pending"
            job["device_id"] = ""
            job["mixed_device_allowed"] = True
            estimates = _work_estimates(
                str(job["algorithm"]), str(job["top_k_level"])
            )
            job["trial_work_estimates"] = {
                str(trial_id): estimates.get(str(trial_id), 1.0)
                for trial_id in job["trial_ids"]
            }
        elif (
            job["mission"] == "collaborative"
            and job["status"] in {"pending", "running"}
        ):
            job["status"] = "pending"
            job["device_id"] = (
                "" if bayesian_terminal else "__after_bayesian_hold__"
            )

    schedule["active_trials"] = {}
    for device in schedule.get("devices", {}).values():
        device["status"] = "idle"
        device["condition_id"] = ""
        device.pop("trial_id", None)
        device.pop("run_generation", None)
    schedule["phase"] = "collaborative" if bayesian_terminal else "bayesian"
    schedule["status"] = "paused"
    schedule["core_condition_count"] = sum(
        not bool(job.get("excluded_from_core")) for job in schedule["jobs"]
    )
    schedule["core_mission_run_count"] = sum(
        len(job["trial_ids"])
        for job in schedule["jobs"]
        if not job.get("excluded_from_core")
    )
    schedule["execution_policy"] = {
        "scheduler": "whole_trial_sharded_lpt_v1",
        "bayesian_top_k_priority": [
            "100%", "75%", "50%", "25%", "10%", "5%", "3%", "1%"
        ],
        "reserve_one_worker_for_highest_bayesian_top_k": True,
        "collaborative_after_bayesian": True,
        "watchdog": {
            "calibration": "online_confirmation",
            "no_movement_events": NO_MOVEMENT_EVENTS,
            "no_progress_events": NO_PROGRESS_EVENTS,
            "repeated_state_count": REPEATED_STATE_COUNT,
            "repeated_state_window": REPEATED_STATE_WINDOW,
            "short_confirmation_events": SHORT_CONFIRMATION_EVENTS,
            "progress_confirmation_events": PROGRESS_CONFIRMATION_EVENTS,
            "event_cap": EVENT_CAP,
        },
    }

    old_implementation = manifest["implementation"]
    build_manifest_path = Path(
        str(old_implementation["device_build"]["manifest_path"])
    )
    new_implementation = implementation_provenance(build_manifest_path.parent)
    if (
        new_implementation["device_build"]["build_id"]
        != old_implementation["device_build"]["build_id"]
    ):
        raise ValueError("cutover unexpectedly changed the device build")
    cutover_epoch = time.time()
    cutover_time = datetime.now(timezone.utc).isoformat()
    prior_segments = list(manifest.get("implementation_segments", []))
    if not prior_segments:
        prior_segments.append(
            {
                "started_at_epoch": 0,
                "ended_at_epoch": cutover_epoch,
                "implementation": old_implementation,
                "label": "pre_watchdog_cutover",
            }
        )
    if (
        prior_segments
        and prior_segments[-1].get("label")
        == "trial_sharding_watchdog_cutover"
    ):
        prior_segments[-1]["implementation"] = new_implementation
        cutover_epoch = float(prior_segments[-1]["started_at_epoch"])
    else:
        prior_segments.append(
            {
                "started_at_epoch": cutover_epoch,
                "ended_at_epoch": None,
                "implementation": new_implementation,
                "label": "trial_sharding_watchdog_cutover",
            }
        )
    manifest["implementation"] = new_implementation
    manifest["implementation_segments"] = prior_segments
    if not any(
        item.get("reason") == "bayesian_k1_exclusion_and_online_watchdog"
        for item in manifest.setdefault("cutovers", [])
    ):
        manifest["cutovers"].append(
            {
                "at": cutover_time,
                "reason": "bayesian_k1_exclusion_and_online_watchdog",
                "archive": str(archive.resolve()),
            }
        )
    schedule["implementation"] = new_implementation
    schedule["implementation_segments"] = prior_segments
    _write_text_atomic(
        root / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    save_schedule(root, schedule)

    archived_completed = sum(len(job.get("completed_trials", [])) for job in k1_jobs)
    existing_archive = (
        json.loads(archive_marker.read_text(encoding="utf-8"))
        if archive_marker.exists() else {}
    )
    reports_archive = archive / "reports_before_cutover"
    if not existing_archive.get("reports_archived"):
        reports_archive.mkdir(parents=True, exist_ok=True)
        for report in sorted((root / "reports").glob("*")):
            if report.is_file():
                destination = reports_archive / report.name
                if destination.exists():
                    destination.unlink()
                shutil.move(str(report), str(destination))
    archive_manifest = {
        "schema": 1,
        "archive": ARCHIVE_NAME,
        "created_at": existing_archive.get(
            "created_at", datetime.now(timezone.utc).isoformat()
        ),
        "reason": EXCLUSION_REASON,
        "condition_ids": sorted(condition_ids),
        "completed_trial_count": archived_completed,
        "record_count": record_count,
        "records_sha256": sha256_file(records_path),
        "schedule_before_cutover_sha256": sha256_file(schedule_archive),
        "manifest_before_cutover_sha256": sha256_file(old_manifest_path),
        "host_source_sha256_after_cutover": new_implementation[
            "host_source"
        ]["sha256"],
        "source_journals_preserved": True,
        "excluded_from_core_reports": True,
        "reports_archived": True,
        "reports_before_cutover": str(reports_archive.resolve()),
    }
    _write_text_atomic(
        archive / "manifest.json",
        json.dumps(archive_manifest, indent=2, sort_keys=True) + "\n",
    )
    _write_text_atomic(
        archive / "README.md",
        "# Archived Bayesian K=1 HIL records\n\n"
        "These six conditions were removed from the primary hardware study on "
        "2026-08-01. Their raw append-only source journals remain untouched. "
        "This directory contains the filtered K=1 records and the schedule "
        "snapshot taken before exclusion. These records are audit evidence only "
        "and are omitted from core and combined reports.\n",
    )
    return archive_manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("campaign", type=Path)
    args = parser.parse_args()
    print(json.dumps(apply_cutover(args.campaign), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
