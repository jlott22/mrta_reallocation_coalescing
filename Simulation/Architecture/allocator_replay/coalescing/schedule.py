"""Immutable schedule preparation and provenance verification."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CampaignConfig, REPOSITORY_ROOT, safe_id_component
from .io import atomic_json, load_json
from .manifests import canonical_sha256, load_all_pairs, sha256_file


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_metadata() -> dict[str, Any]:
    def run(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else "unavailable"

    status = run("status", "--short")
    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": status not in ("", "unavailable"),
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def _source_tree_sha256() -> str:
    roots = (
        Path(__file__).resolve().parent,
        Path(__file__).resolve().parents[1] / "device" / "native" / "collaborative",
        Path(__file__).resolve().parents[1] / "device" / "physical",
        Path(__file__).resolve().parents[1] / "device" / "common",
        Path(__file__).resolve().parents[1] / "host",
    )
    digest = hashlib.sha256()
    paths = sorted(path for root in roots for path in root.rglob("*.py"))
    for path in paths:
        relative = path.resolve().relative_to(REPOSITORY_ROOT.resolve())
        digest.update(str(relative).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def campaign_root(config: CampaignConfig, campaign_id: str | None = None) -> Path:
    name = safe_id_component(
        campaign_id or config.campaign_id, "campaign_id"
    )
    results_root = config.results_root.resolve()
    root = (results_root / name).resolve()
    try:
        relative = root.relative_to(results_root)
    except ValueError as exc:  # pragma: no cover - defensive after SAFE_ID
        raise ValueError("campaign root escaped configured results_root") from exc
    if len(relative.parts) != 1:
        raise ValueError("campaign_id must select exactly one output directory")
    return root


def _verify_device_binding(binding: dict[str, Any]) -> None:
    claimed = str(binding.get("device_binding_sha256", ""))
    unsigned = dict(binding)
    unsigned.pop("device_binding_sha256", None)
    if not claimed or canonical_sha256(unsigned) != claimed:
        raise ValueError("campaign device binding hash mismatch")
    for field in (
        "execution_mode",
        "build_id",
        "source_bundle_sha256",
        "module_set_sha256",
        "build_manifest_sha256",
        "build_root",
    ):
        if field not in binding:
            raise ValueError(f"campaign device binding is missing {field}")
    devices = binding.get("devices")
    if not isinstance(devices, list) or not devices:
        raise ValueError("campaign device binding has no devices")
    ids = [str(item.get("device_id", "")) for item in devices]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("campaign device binding has invalid device IDs")


def _initial_state(schedule: dict[str, Any]) -> dict[str, Any]:
    conditions: dict[str, Any] = {}
    for condition in schedule["conditions"]:
        trials = {
            item["paired_manifest_id"]: {
                "status": "pending",
                "generation": 0,
                "device_id": None,
                "device_binding_sha256": schedule["device_binding"][
                    "device_binding_sha256"
                ],
                "completed_device_identity": None,
                "last_error": "",
            }
            for item in condition["trials"]
        }
        conditions[condition["condition_id"]] = {
            "status": "pending",
            "trials": trials,
        }
    return {
        "schema_version": 1,
        "campaign_id": schedule["campaign_id"],
        "schedule_sha256": schedule["schedule_sha256"],
        "device_binding_sha256": schedule["device_binding"][
            "device_binding_sha256"
        ],
        "status": "prepared",
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "conditions": conditions,
    }


def prepare_campaign(
    config: CampaignConfig,
    *,
    campaign_id: str | None = None,
    device_binding: dict[str, Any],
) -> Path:
    _verify_device_binding(device_binding)
    root = campaign_root(config, campaign_id)
    schedule_path = root / "schedule.json"
    if schedule_path.exists():
        verify_campaign(root, config, device_binding=device_binding)
        return root

    pairs = load_all_pairs(config)
    for (load_id, trace_id), pair in pairs.items():
        if pair.arrival_rate_tasks_per_s != config.arrival_rate(load_id):
            raise ValueError(
                f"configured rate for {load_id} differs from paired release "
                f"manifest {trace_id}"
            )
    condition_rows: list[dict[str, Any]] = []
    for condition in config.conditions():
        trials = [
            pairs[(condition.arrival_load, trace_id)].schedule_row()
            for trace_id in config.trace_ids
        ]
        condition_rows.append({**condition.as_dict(), "trials": trials})
    manifest_index = config.manifest_root / "manifest_index.json"
    resolved_campaign_id = safe_id_component(
        campaign_id or config.campaign_id, "campaign_id"
    )
    schedule: dict[str, Any] = {
        "schema_version": 2,
        "study_id": "mrta_reallocation_coalescing",
        "campaign_mode": "motionless_arrival_epoch_allocator_replay",
        "campaign_id": resolved_campaign_id,
        "created_at": _utc_now(),
        "mission": "collaborative",
        "candidate_mode": "unrestricted",
        "max_candidate_cells": None,
        "allocator_rounds_per_epoch": config.allocator_rounds_per_epoch,
        "timeout_seconds": config.timeout_seconds,
        "manifest_root": str(config.manifest_root),
        "manifest_set_id": config.manifest_set_id,
        "manifest_index_sha256": sha256_file(manifest_index),
        "config_source": str(config.source_path),
        "config_sha256": sha256_file(config.source_path),
        "conditions": condition_rows,
        "device_binding": device_binding,
        "provenance": {
            "git": _git_metadata(),
            "source_tree_sha256": _source_tree_sha256(),
            "python": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "hostname": platform.node(),
        },
        "scientific_scope": {
            "measures": "allocator execution for arrival-admission epochs",
            "does_not_measure": "movement or mission completion",
            "mandatory_mission_epochs": "owned by the paired simulator campaign",
            "trace_end_flush": "validation-only guard for incomplete pure-count batches",
        },
    }
    schedule["schedule_sha256"] = canonical_sha256(schedule)
    root.mkdir(parents=True, exist_ok=False)
    atomic_json(schedule_path, schedule)
    atomic_json(root / "config_snapshot.json", config.as_dict())
    atomic_json(root / "state.json", _initial_state(schedule))
    return root


def verify_campaign(
    root: Path,
    config: CampaignConfig | None = None,
    *,
    device_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    if config is not None and root != campaign_root(config, root.name):
        raise ValueError("campaign root is outside configured results_root")
    schedule_path = root / "schedule.json"
    state_path = root / "state.json"
    schedule = load_json(schedule_path)
    claimed = str(schedule.get("schedule_sha256", ""))
    unsigned = dict(schedule)
    unsigned.pop("schedule_sha256", None)
    if canonical_sha256(unsigned) != claimed:
        raise ValueError("campaign schedule hash mismatch")
    if schedule.get("study_id") != "mrta_reallocation_coalescing":
        raise ValueError("campaign belongs to another study")
    if schedule.get("mission") != "collaborative":
        raise ValueError("active HIL campaign must be Collaborative Visit")
    if schedule.get("candidate_mode") != "unrestricted":
        raise ValueError("active HIL campaign must use unrestricted candidates")
    if schedule.get("max_candidate_cells") is not None:
        raise ValueError("active HIL campaign contains a candidate restriction")
    scheduled_binding = schedule.get("device_binding")
    if not isinstance(scheduled_binding, dict):
        raise ValueError("campaign schedule has no device binding")
    _verify_device_binding(scheduled_binding)
    if device_binding is not None:
        _verify_device_binding(device_binding)
        if device_binding != scheduled_binding:
            raise ValueError(
                "current build/module/firmware identity differs from schedule"
            )
    expected_source = schedule.get("provenance", {}).get("source_tree_sha256")
    if expected_source != _source_tree_sha256():
        raise ValueError(
            "HIL source changed after campaign preparation; use a new campaign_id"
        )
    manifest_index = Path(schedule["manifest_root"]) / "manifest_index.json"
    if sha256_file(manifest_index) != schedule["manifest_index_sha256"]:
        raise ValueError("generated manifest index changed after campaign preparation")
    for condition in schedule["conditions"]:
        for trial in condition["trials"]:
            for name in ("scenario", "release"):
                path = Path(trial[f"{name}_path"])
                if sha256_file(path) != trial[f"{name}_sha256"]:
                    raise ValueError(f"paired {name} manifest changed: {path}")
    if config is not None:
        if sha256_file(config.source_path) != schedule["config_sha256"]:
            raise ValueError("campaign config changed; use a new campaign_id")
        if config.manifest_root != Path(schedule["manifest_root"]):
            raise ValueError("campaign manifest root differs from config")
    state = load_json(state_path)
    if state.get("schedule_sha256") != claimed:
        raise ValueError("campaign state belongs to a different schedule")
    if state.get("device_binding_sha256") != scheduled_binding.get(
        "device_binding_sha256"
    ):
        raise ValueError("campaign state belongs to a different device binding")
    bound_devices = {
        str(item["device_id"]): item for item in scheduled_binding["devices"]
    }
    for condition in state.get("conditions", {}).values():
        for trial in condition.get("trials", {}).values():
            if trial.get("device_binding_sha256") != scheduled_binding.get(
                "device_binding_sha256"
            ):
                raise ValueError("trial state belongs to a different device binding")
            pinned = trial.get("device_id")
            if pinned is not None and str(pinned) not in bound_devices:
                raise ValueError("trial is pinned outside the device binding")
            if trial.get("status") == "completed":
                if trial.get("completed_device_identity") != bound_devices.get(
                    str(pinned)
                ):
                    raise ValueError(
                        "completed trial lacks its exact build/module/firmware identity"
                    )
    return schedule
