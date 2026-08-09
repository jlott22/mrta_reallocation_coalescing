#!/usr/bin/env python3
"""Run one partition of the known-visit grid-density sensitivity condition manifest."""
from __future__ import annotations

import argparse
import csv
import inspect
import json
import random
import sys
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List

from known_visit_sim.algorithms.registry import load_allocator_class
from known_visit_sim.config import SimConfig, edge_even_start_positions, generate_robot_ids
from known_visit_sim.core.scenario_loader import load_scenarios
from known_visit_sim.core.scheduler import AsyncTrialRunner
from known_visit_sim.metrics.export import write_outputs
from known_visit_sim.metrics.summary import build_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--worker-index", type=int, required=True)
    parser.add_argument("--num-workers", type=int, required=True)
    parser.add_argument("--force", action="store_true", help="Rerun even if output CSV already appears complete.")
    return parser.parse_args()


def count_data_rows(csv_path: Path) -> int:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return 0
    with csv_path.open(newline="") as f:
        return max(0, sum(1 for _ in f) - 1)


def should_skip(row: Dict[str, str], force: bool) -> bool:
    if force:
        return False
    out_dir = Path(row["out_dir"])
    expected = int(row["num_trials"])
    expected_robot_rows = expected * int(row["robot_count"])
    expected_target_rows = expected * int(row.get("num_targets", "10"))
    return (
        count_data_rows(out_dir / "system_performance.csv") == expected
        and count_data_rows(out_dir / "trial_summary.csv") == expected
        and count_data_rows(out_dir / "robot_performance.csv") == expected_robot_rows
        and count_data_rows(out_dir / "target_performance.csv") == expected_target_rows
    )


def _instantiate(cls: Any, *args: Any, **kwargs: Any) -> Any:
    """Instantiate a class while tolerating slightly different constructor names."""
    try:
        return cls(**kwargs)
    except TypeError:
        pass
    try:
        return cls(*args)
    except TypeError:
        pass
    filtered = {}
    try:
        params = inspect.signature(cls).parameters
        filtered = {k: v for k, v in kwargs.items() if k in params}
        if filtered:
            return cls(**filtered)
    except Exception:
        pass
    return cls()


def make_comm_model(model_name: str, comm_level: str, seed: int) -> Any:
    import known_visit_sim.comms.models as models

    name = model_name.strip().lower()
    level = float(comm_level)
    rng_seed = int(seed)

    if name == "ideal":
        return _instantiate(getattr(models, "IdealModel"))

    if name == "bernoulli":
        cls = getattr(models, "BernoulliModel")
        return _instantiate(cls, level, drop_prob=level, seed=rng_seed)

    if name == "gilbert_elliott":
        return models.make_comm_model("gilbert_elliott", level)

    if name == "rayleigh_style":
        for cls_name in ("RayleighStyleModel", "RayleighModel", "RayleighFadingModel"):
            cls = getattr(models, cls_name, None)
            if cls is not None:
                return _instantiate(
                    cls,
                    level,
                    sensitivity_dbm=level,
                    comm_level=level,
                    seed=rng_seed,
                )
        raise RuntimeError("known_visit_sim.comms.models has no Rayleigh-style model class")

    raise ValueError(f"unsupported comm_model: {model_name}")


def make_config(row: Dict[str, str]) -> SimConfig:
    grid_size = int(row["grid_size"])
    robot_count = int(row["robot_count"])
    ids = generate_robot_ids(robot_count)
    values: Dict[str, Any] = {
        "grid_size": grid_size,
        "robot_ids": ids,
        "start_positions": edge_even_start_positions(grid_size, ids),
        "comm_delay_s": 0.04,
        "comm_delay_jitter_s": 0.01,
        "collision_intent_settle_s": 0.10,
        "debug_max_events": 800_000,
        "condition_id": row["condition_id"],
        "target_cells_per_robot": float(row["target_cells_per_robot"]),
        "actual_cells_per_robot": float(row["actual_cells_per_robot"]),
    }

    # Filter to fields accepted by the local SimConfig version.
    if is_dataclass(SimConfig):
        allowed = {f.name for f in fields(SimConfig)}
        values = {k: v for k, v in values.items() if k in allowed}
    else:
        try:
            allowed = set(inspect.signature(SimConfig).parameters)
            values = {k: v for k, v in values.items() if k in allowed}
        except Exception:
            pass
    return SimConfig(**values)


def add_backfill(rows: List[Dict[str, Any]], cond: Dict[str, str]) -> None:
    for row in rows:
        row.setdefault("condition_id", cond["condition_id"])
        row.setdefault("grid_size", cond["grid_size"])
        row.setdefault("grid_cells", cond["grid_cells"])
        row.setdefault("target_cells_per_robot", cond["target_cells_per_robot"])
        row.setdefault("robot_count", cond["robot_count"])
        row.setdefault("actual_cells_per_robot", cond["actual_cells_per_robot"])
        row.setdefault("num_targets", cond.get("num_targets", "10"))
        row.setdefault("comm_model", cond["comm_model"])
        row.setdefault("comm_level", cond["comm_level"])
        row.setdefault("algorithm", cond["algorithm_name"])
        row.setdefault("algorithm_name", cond["algorithm_name"])
        row.setdefault("scenario_file", cond["scenario_file"])


def run_condition(row: Dict[str, str]) -> None:
    out_dir = Path(row["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    for filename in (
        "system_performance.csv",
        "trial_summary.csv",
        "robot_performance.csv",
        "target_performance.csv",
        "config_used.json",
    ):
        stale = out_dir / filename
        if stale.exists():
            stale.unlink()

    cfg = make_config(row)
    starts = set(edge_even_start_positions(int(row["grid_size"]), generate_robot_ids(int(row["robot_count"]))).values())
    scenarios = load_scenarios(Path(row["scenario_file"]), int(row["grid_size"]), starts)
    scenarios = scenarios[: int(row["num_trials"])]

    allocator_cls = load_allocator_class(row["algorithm_name"])
    comm_model = make_comm_model(row["comm_model"], row["comm_level"], int(row["seed"]))

    trial_rows: List[Dict[str, Any]] = []
    system_rows: List[Dict[str, Any]] = []
    robot_rows: List[Dict[str, Any]] = []
    target_rows: List[Dict[str, Any]] = []

    for idx, scenario in enumerate(scenarios):
        trial_seed = int(row["seed"]) + idx
        random.seed(trial_seed)
        state = AsyncTrialRunner(cfg, allocator_cls, comm_model, trial_seed).run_trial(scenario)
        trial, system, robots, targets = build_rows(
            state,
            row["algorithm_name"],
            row["comm_model"],
            row["comm_level"],
            row["scenario_file"],
        )
        for collection in ([trial], [system], robots, targets):
            add_backfill(collection, row)
        trial_rows.append(trial)
        system_rows.append(system)
        robot_rows.extend(robots)
        target_rows.extend(targets)
        print(
            f"trial {getattr(scenario, 'trial_id', idx)} complete: "
            f"all_targets_visited={system.get('all_targets_visited', '')} "
            f"max_robot_steps={system.get('max_robot_steps', system.get('steps_total', ''))}",
            flush=True,
        )

    config_out = {
        "condition": dict(row),
        "num_scenarios_run": len(scenarios),
    }
    write_outputs(out_dir, trial_rows, system_rows, robot_rows, target_rows, config_out)


def main() -> None:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    manifest = Path(args.manifest).resolve()
    if args.num_workers <= 0:
        raise SystemExit("--num-workers must be positive")
    if not 0 <= args.worker_index < args.num_workers:
        raise SystemExit("--worker-index must be in [0, num-workers)")

    with manifest.open(newline="") as f:
        rows = list(csv.DictReader(f))

    selected = [row for i, row in enumerate(rows) if i % args.num_workers == args.worker_index]
    print(f"[WORKER {args.worker_index:02d}] selected {len(selected)} of {len(rows)} conditions")

    failed: List[str] = []
    for local_idx, row in enumerate(selected, start=1):
        out_dir = Path(row["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "run.log"
        if should_skip(row, args.force):
            print(f"[WORKER {args.worker_index:02d}] skip complete {row['condition_id']} ({local_idx}/{len(selected)})")
            continue

        print(f"[WORKER {args.worker_index:02d}] run {row['condition_id']} ({local_idx}/{len(selected)})")
        try:
            # Mirror stdout/stderr into the per-condition log for debugging.
            original_stdout, original_stderr = sys.stdout, sys.stderr
            with log_path.open("w") as log:
                log.write(json.dumps({"condition": row}, indent=2) + "\n\n")
                sys.stdout = log
                sys.stderr = log
                try:
                    run_condition(row)
                finally:
                    sys.stdout = original_stdout
                    sys.stderr = original_stderr
        except Exception as exc:
            sys.stdout = getattr(sys, "__stdout__", sys.stdout)
            sys.stderr = getattr(sys, "__stderr__", sys.stderr)
            print(f"[WORKER {args.worker_index:02d}] ERROR {row['condition_id']}: {exc}", file=sys.stderr)
            print(f"[WORKER {args.worker_index:02d}] see log: {log_path}", file=sys.stderr)
            failed.append(row["condition_id"])

    if failed:
        print(f"[WORKER {args.worker_index:02d}] failed conditions: {','.join(failed)}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[WORKER {args.worker_index:02d}] done")


if __name__ == "__main__":
    main()
