"""Run one immutable online Collaborative Visit condition.

This is the subprocess boundary used by :mod:`study.campaign`.  It accepts no
candidate-limit option: every admitted task is passed to the retained allocator.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from known_visit_sim.algorithms.registry import load_allocator_class
from known_visit_sim.comms.models import make_comm_model
from known_visit_sim.config import SimConfig
from known_visit_sim.core.reallocation import ReallocationPolicy
from known_visit_sim.core.scheduler import AsyncTrialRunner
from known_visit_sim.core.types import TrialScenario


HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _expected_hash(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not HEX64.fullmatch(normalized):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
    return normalized


def _task_rows(value: Any, *, require_release: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("manifest tasks must be a non-empty list")
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_cells: set[tuple[int, int]] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("manifest task rows must be objects")
        task_id = str(item["task_id"])
        cell = int(item["x"]), int(item["y"])
        if task_id in seen_ids:
            raise ValueError(f"duplicate task ID: {task_id}")
        if cell in seen_cells:
            raise ValueError(f"duplicate task cell: {cell}")
        seen_ids.add(task_id)
        seen_cells.add(cell)
        initially_visible = item["initially_visible"]
        if not isinstance(initially_visible, bool):
            raise ValueError(f"initially_visible must be boolean for {task_id}")
        row = {
            "task_id": task_id,
            "x": cell[0],
            "y": cell[1],
            "initially_visible": initially_visible,
        }
        if require_release:
            release_s = float(item["release_time_s"])
            if not math.isfinite(release_s) or release_s < 0.0:
                raise ValueError(f"invalid release time for {task_id}")
            row["release_time_s"] = release_s
        rows.append(row)
    return rows


def load_paired_manifests(
    scenario_path: Path,
    release_path: Path,
    scenario_sha256: str,
    release_sha256: str,
    trace_id: str,
    runtime_seed: int,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Byte- and semantics-validate one paired input before simulation."""

    scenario_sha256 = _expected_hash(scenario_sha256, "scenario_sha256")
    release_sha256 = _expected_hash(release_sha256, "release_sha256")
    if _sha256(scenario_path) != scenario_sha256:
        raise ValueError("scenario manifest SHA-256 mismatch")
    if _sha256(release_path) != release_sha256:
        raise ValueError("release manifest SHA-256 mismatch")
    scenario = _load_object(scenario_path)
    release = _load_object(release_path)
    if scenario.get("schema_version") != 1 or release.get("schema_version") != 1:
        raise ValueError("unsupported manifest schema version")
    if scenario.get("manifest_kind") != "scenario":
        raise ValueError("wrong scenario manifest kind")
    if release.get("manifest_kind") != "release_trace":
        raise ValueError("wrong release manifest kind")
    if str(scenario.get("trace_id")) != trace_id or str(release.get("trace_id")) != trace_id:
        raise ValueError("selected trial ID differs from paired manifests")
    if int(scenario.get("runtime_seed")) != int(runtime_seed):
        raise ValueError("CLI seed differs from scenario runtime seed")
    if int(release.get("runtime_seed")) != int(runtime_seed):
        raise ValueError("CLI seed differs from release runtime seed")
    if str(release.get("scenario_sha256", "")).lower() != scenario_sha256:
        raise ValueError("release manifest is linked to another scenario")
    spatial = _task_rows(scenario.get("tasks"), require_release=False)
    dynamic = _task_rows(release.get("tasks"), require_release=True)
    if [row["task_id"] for row in spatial] != [row["task_id"] for row in dynamic]:
        raise ValueError("paired task ID ordering differs")
    for left, right in zip(spatial, dynamic, strict=True):
        for name in ("task_id", "x", "y", "initially_visible"):
            if left[name] != right[name]:
                raise ValueError(f"paired task differs in {name}: {left['task_id']}")
        if right["initially_visible"] != (right["release_time_s"] == 0.0):
            raise ValueError(f"initial visibility disagrees with release time: {right['task_id']}")
    times = [row["release_time_s"] for row in dynamic]
    if times != sorted(times):
        raise ValueError("release task rows must be ordered by absolute release time")
    expected_initial = sum(row["initially_visible"] for row in dynamic)
    if int(release.get("initial_task_count", expected_initial)) != expected_initial:
        raise ValueError("initial_task_count disagrees with task rows")
    return scenario, release, dynamic


def _numeric_trial_id(trace_id: str) -> int:
    match = re.search(r"(\d+)$", trace_id)
    if match:
        return int(match.group(1))
    return int.from_bytes(hashlib.sha256(trace_id.encode()).digest()[:4], "big")


def _normalize_csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    values = list(rows)
    fields: list[str] = []
    seen: set[str] = set()
    for row in values:
        for field in row:
            if field not in seen:
                fields.append(field)
                seen.add(field)
    if not fields:
        raise ValueError(f"refusing to write an empty CSV: {path.name}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in values:
            writer.writerow({name: _normalize_csv_value(row.get(name)) for name in fields})
    os.replace(temporary, path)


def _git_metadata(repo_root: Path) -> dict[str, Any]:
    def run(*arguments: str) -> str:
        try:
            result = subprocess.run(
                ["git", *arguments], cwd=repo_root, text=True,
                capture_output=True, check=True, timeout=15,
            )
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "unavailable"

    status = run("status", "--porcelain=v1")
    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": status not in {"", "unavailable"},
    }


def _policy(args: argparse.Namespace) -> ReallocationPolicy:
    if args.policy == "eager":
        if args.batch_size != 1 or args.max_pending_age_s is not None:
            raise ValueError("eager requires B=1 and no W")
        return ReallocationPolicy.eager()
    if args.policy == "count":
        if args.max_pending_age_s is not None:
            raise ValueError("pure count does not accept W")
        return ReallocationPolicy.count(args.batch_size)
    if args.max_pending_age_s is None:
        raise ValueError("bounded policy requires W")
    return ReallocationPolicy.bounded(args.batch_size, args.max_pending_age_s)


def run(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    source_git = _git_metadata(repo_root)
    scenario_path = args.scenario_manifest.resolve()
    release_path = args.release_manifest.resolve()
    scenario, release, tasks = load_paired_manifests(
        scenario_path,
        release_path,
        args.scenario_sha256,
        args.release_sha256,
        args.trial_id,
        args.seed,
    )
    if str(release.get("load_id")) != args.arrival_load:
        raise ValueError("CLI arrival load differs from release manifest")
    expected_hash_seed = int(args.seed) % (2 ** 32)
    hash_seed_text = os.environ.get("PYTHONHASHSEED")
    if hash_seed_text is None:
        raise ValueError(
            "PYTHONHASHSEED must be set before interpreter startup; use study.campaign"
        )
    try:
        python_hash_seed = int(hash_seed_text)
    except ValueError as error:
        raise ValueError("PYTHONHASHSEED must be an integer for paired replay") from error
    if python_hash_seed != expected_hash_seed:
        raise ValueError("PYTHONHASHSEED differs from the paired runtime seed")
    starts = scenario.get("robot_starts")
    if not isinstance(starts, list) or not starts:
        raise ValueError("scenario must contain robot starts")
    robot_ids: list[str] = []
    start_positions: dict[str, tuple[int, int]] = {}
    start_headings: dict[str, tuple[int, int]] = {}
    for item in starts:
        rid = str(item["robot_id"])
        if rid in start_positions:
            raise ValueError(f"duplicate robot start ID: {rid}")
        robot_ids.append(rid)
        start_positions[rid] = int(item["x"]), int(item["y"])
        start_headings[rid] = int(item.get("heading_x", 1)), int(item.get("heading_y", 0))
    grid_size = int(scenario["grid_size"])
    targets = [(row["x"], row["y"]) for row in tasks]
    start_cells = set(start_positions.values())
    if any(not (0 <= x < grid_size and 0 <= y < grid_size) for x, y in targets):
        raise ValueError("task coordinate lies outside the grid")
    if start_cells.intersection(targets):
        raise ValueError("task overlaps a robot start")
    trial = TrialScenario(
        trial_id=_numeric_trial_id(args.trial_id),
        targets=targets,
        metadata={
            "trace_id": args.trial_id,
            "runtime_seed": args.seed,
            "task_ids": [row["task_id"] for row in tasks],
            "scenario_sha256": args.scenario_sha256.lower(),
            "release_sha256": args.release_sha256.lower(),
        },
    )
    cfg = SimConfig(
        grid_size=grid_size,
        robot_ids=robot_ids,
        start_positions=start_positions,
        start_headings=start_headings,
        robot_start_layout="manifest",
        condition_id=args.condition_id,
        commitment_horizon=None,
        max_candidate_cells=None,
    )
    allocator_cls = load_allocator_class(args.algorithm)
    policy = _policy(args)
    release_times = {cell: row["release_time_s"] for cell, row in zip(targets, tasks, strict=True)}
    state = AsyncTrialRunner(
        cfg,
        allocator_cls,
        make_comm_model("ideal", None),
        seed=args.seed,
    ).run_online_trial(trial, release_times, policy)
    state.validate_online_invariants()
    metrics = state.online_metrics()
    dimensions = {
        "trial_id": args.trial_id,
        "condition_id": args.condition_id,
        "algorithm": str(getattr(allocator_cls, "name", args.algorithm)).upper(),
        "arrival_load": args.arrival_load,
        "policy_id": args.policy_id,
        "policy_mode": policy.mode,
        "policy_max_pending_age_s": policy.max_pending_age_s,
        "policy": policy.mode,
        "batch_size": policy.batch_size,
        "max_pending_age_s_configured": policy.max_pending_age_s,
        "candidate_mode": "unrestricted",
        "max_candidate_cells": None,
        "grid_size": grid_size,
        "robot_count": len(robot_ids),
        "task_count": len(tasks),
        "initial_task_count": sum(row["initially_visible"] for row in tasks),
        "runtime_seed": args.seed,
        "python_hash_seed": python_hash_seed,
        "scenario_manifest": str(scenario_path),
        "release_manifest": str(release_path),
        "scenario_sha256": args.scenario_sha256.lower(),
        "release_sha256": args.release_sha256.lower(),
    }
    summary = {
        "schema_version": 1,
        "trial_status": "completed",
        **dimensions,
        **metrics,
        "trial_id_numeric": metrics["trial_id"],
        "trial_id": args.trial_id,
        "timing_definition": {
            "simulated_execution_time_s": "existing asynchronous event clock; movement, turns, waits, replans, and legitimate simulated execution delays",
            "cumulative_allocator_time_s": "sum of measured choose_goal host durations for all robots",
            "allocator_parallel_critical_path_time_s": "for calls sharing an epoch and simulated timestamp, sum per robot then take the maximum; sum those group maxima",
            "mission_elapsed_time_serial_compute_s": "simulated_execution_time_s + cumulative_allocator_time_s; conservative team-serial sensitivity metric",
            "mission_elapsed_time_s": "simulated_execution_time_s + allocator_parallel_critical_path_time_s; deployment-facing four-processor estimate",
            "host_program_runtime_s": "raw runner duration; diagnostic only",
        },
    }
    task_rows = [{**dimensions, **row, "trial_id": args.trial_id} for row in state.task_rows()]
    epoch_rows = [{**dimensions, **row, "trial_id": args.trial_id} for row in state.epoch_rows()]
    call_rows = [{**dimensions, **row, "trial_id": args.trial_id} for row in state.allocator_call_rows()]
    queue_rows = [{**dimensions, **row, "trial_id": args.trial_id} for row in state.queue_sample_rows()]
    if len(task_rows) != len(tasks) or not epoch_rows:
        raise AssertionError("online output cardinality invariant failed")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    destinations = {
        name: output / name
        for name in (
            "trial_summary.json", "task_events.csv", "allocation_epochs.csv",
            "allocator_calls.csv", "pending_queue_samples.csv", "run_metadata.json",
        )
    }
    conflicts = [path for path in destinations.values() if path.exists()]
    if conflicts:
        raise FileExistsError("refusing to overwrite online outputs: " + ", ".join(map(str, conflicts)))
    _atomic_json(destinations["trial_summary.json"], summary)
    _atomic_csv(destinations["task_events.csv"], task_rows)
    _atomic_csv(destinations["allocation_epochs.csv"], epoch_rows)
    _atomic_csv(destinations["allocator_calls.csv"], call_rows)
    _atomic_csv(destinations["pending_queue_samples.csv"], queue_rows or [{**dimensions, "event": "none", "depth": 0, "oldest_age_s": 0.0}])
    _atomic_json(destinations["run_metadata.json"], {
        "schema_version": 1,
        "command": sys.argv,
        "git": source_git,
        "machine": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": sys.version,
            "logical_cores": os.cpu_count(),
        },
        "summary_sha256": _sha256(destinations["trial_summary.json"]),
    })
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-manifest", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--scenario-sha256", required=True)
    parser.add_argument("--release-sha256", required=True)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--condition-id", required=True)
    parser.add_argument("--algorithm", required=True)
    parser.add_argument("--arrival-load", required=True)
    parser.add_argument("--policy-id", required=True)
    parser.add_argument("--policy", choices=("eager", "count", "bounded"), required=True)
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--max-pending-age-s", type=float)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = run(args)
    except Exception as exc:
        try:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            _atomic_json(args.output_dir / "trial_failure.json", {
                "trial_status": "failed",
                "failure_type": type(exc).__name__,
                "failure_message": str(exc),
            })
        except Exception:
            pass
        print(f"online trial failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        f"completed {summary['condition_id']} {summary['trial_id']}: "
        f"tasks={summary['task_count']} steps={summary['total_team_steps']} "
        f"epochs={summary['allocation_epoch_count']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
