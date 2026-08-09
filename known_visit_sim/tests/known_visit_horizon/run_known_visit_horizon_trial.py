#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, List, Tuple

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = Path(os.environ.get("DCTA_REPO_ROOT", DEFAULT_REPO_ROOT)).expanduser().resolve()
if not (REPO_ROOT / "known_visit_sim").is_dir():
    raise RuntimeError(
        f"known_visit_sim package not found under {REPO_ROOT}; "
        "set DCTA_REPO_ROOT to the repository checkout"
    )
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from known_visit_sim.algorithms.registry import load_allocator_class
from known_visit_sim.config import SimConfig, edge_even_start_positions, generate_robot_ids
from known_visit_sim.core.scenario_loader import load_scenarios
from known_visit_sim.core.scheduler import AsyncTrialRunner
from known_visit_sim.generate_scenarios import generate
from known_visit_sim.metrics.export import write_outputs
from known_visit_sim.metrics.summary import build_rows
from known_visit_sim.comms import models as comm_models

Cell = Tuple[int, int]


def generate_scenario_file(path: Path, grid_size: int, num_trials: int, num_targets: int, num_robots: int, seed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    targets_by_trial = generate(grid_size, num_trials, num_targets, num_robots, seed)
    max_targets = max((len(t) for t in targets_by_trial), default=num_targets)
    fields = ["trial_id"]
    for i in range(1, max_targets + 1):
        fields += [f"target{i}_x", f"target{i}_y"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for trial_id, targets in enumerate(targets_by_trial):
            row: dict[str, Any] = {"trial_id": trial_id}
            for i, (x, y) in enumerate(targets, start=1):
                row[f"target{i}_x"] = x
                row[f"target{i}_y"] = y
            writer.writerow(row)


def _construct(cls: type, *candidates: tuple[tuple[Any, ...], dict[str, Any]]) -> Any:
    errors: list[str] = []
    for args, kwargs in candidates:
        try:
            return cls(*args, **kwargs)
        except TypeError as exc:
            errors.append(str(exc))
    raise TypeError(f"Could not construct {cls.__name__}; tried {len(candidates)} signatures: {errors}")


def make_comm_model(name: str, level: str | None) -> Any:
    name = name.lower()
    if name == "ideal":
        return comm_models.IdealModel()
    if name == "bernoulli":
        drop = float(level if level not in (None, "") else 0.0)
        return comm_models.BernoulliModel(drop)
    if name in {"gilbert_elliott", "gilbert_elliot", "ge", "gilbert-elliot"}:
        delivery_prob = float(level if level not in (None, "") else 0.75)
        return comm_models.make_comm_model("gilbert_elliott", delivery_prob)
    if name in {"rayleigh_style", "rayleigh"}:
        cls = getattr(comm_models, "RayleighStyleModel", None) or getattr(comm_models, "RayleighModel", None)
        if cls is None:
            raise RuntimeError("known_visit_sim.comms.models has no RayleighStyleModel/RayleighModel class")
        sensitivity = float(level if level not in (None, "") else -50.66)
        return _construct(
            cls,
            ((), {"sensitivity_dbm": sensitivity}),
            ((), {"comm_level": sensitivity}),
            ((), {}),
        )
    raise ValueError(f"Unknown comm model: {name}")


def apply_horizon(allocator_cls: type, horizon: int) -> list[str]:
    changed: list[str] = []
    for attr in ("BUNDLE_SIZE", "COMMITMENT_HORIZON"):
        if hasattr(allocator_cls, attr):
            setattr(allocator_cls, attr, int(horizon))
            changed.append(attr)
    return changed


def make_config(args: argparse.Namespace) -> SimConfig:
    ids = generate_robot_ids(args.num_robots)
    values = dict(
        grid_size=args.grid_size,
        robot_ids=ids,
        start_positions=edge_even_start_positions(args.grid_size, ids),
        comm_delay_s=args.comm_delay_s,
        comm_delay_jitter_s=args.comm_delay_jitter_s,
        collision_intent_settle_s=args.collision_intent_settle_s,
        stalled_allocation_recovery_s=args.stalled_allocation_recovery_s,
        debug_max_events=args.debug_max_events,
        debug_max_stagnant_events=args.debug_max_stagnant_events,
        condition_id=args.condition_id,
    )
    return SimConfig(**values)


def load_trial_journal(path: Path) -> list[dict[str, Any]]:
    """Load complete per-trial records; ignore a torn final line after interruption."""
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not all(key in record for key in ("trial", "system", "robots", "targets")):
                    raise ValueError("missing result section")
                records.append(record)
            except (json.JSONDecodeError, ValueError) as exc:
                print(f"[WARN] ignoring invalid journal line {line_number}: {exc}", flush=True)
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one known-visit planning-horizon sensitivity condition.")
    parser.add_argument("--scenario-file", required=True)
    parser.add_argument("--generate-scenarios-if-missing", action="store_true")
    parser.add_argument("--num-trials", type=int, default=300)
    parser.add_argument("--max-trials", type=int, default=None)
    parser.add_argument("--num-targets", type=int, default=10)
    parser.add_argument("--grid-size", type=int, default=19)
    parser.add_argument("--num-robots", type=int, default=4)
    parser.add_argument("--algorithm", required=True, help="Known-visit algorithm name, e.g. ACBBA, PI, HIPC, DMCHBA, DGA")
    parser.add_argument("--comm-model", required=True, choices=["ideal", "bernoulli", "gilbert_elliott", "gilbert_elliot", "rayleigh_style"])
    parser.add_argument("--comm-level", default="")
    parser.add_argument("--commitment-horizon", type=int, required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--condition-id", default="")
    parser.add_argument("--study-type", choices=["horizon", "core"], default="horizon")
    parser.add_argument("--suite-name", default="known_visit_horizon")
    parser.add_argument("--comm-delay-s", type=float, default=0.04)
    parser.add_argument("--comm-delay-jitter-s", type=float, default=0.01)
    parser.add_argument("--collision-intent-settle-s", type=float, default=0.10)
    parser.add_argument("--debug-max-events", type=int, default=20000)
    parser.add_argument("--debug-max-stagnant-events", type=int, default=2000)
    parser.add_argument("--stalled-allocation-recovery-s", type=float, default=120.0)
    args = parser.parse_args()

    scenario_path = Path(args.scenario_file)
    if args.generate_scenarios_if_missing and not scenario_path.exists():
        generate_scenario_file(scenario_path, args.grid_size, args.num_trials, args.num_targets, args.num_robots, args.seed)

    ids = generate_robot_ids(args.num_robots)
    starts = set(edge_even_start_positions(args.grid_size, ids).values())
    scenarios = load_scenarios(scenario_path, args.grid_size, starts)
    if args.max_trials is not None:
        scenarios = scenarios[: args.max_trials]

    allocator_cls = load_allocator_class(args.algorithm)
    changed = apply_horizon(allocator_cls, args.commitment_horizon)
    if not changed:
        print(f"[WARN] {args.algorithm} has no BUNDLE_SIZE or COMMITMENT_HORIZON; horizon is metadata only.")

    cfg = make_config(args)
    comm_level_label = args.comm_level if args.comm_level != "" else "1.0"

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    journal_path = out_dir / "_trial_journal.jsonl"
    journal_records = load_trial_journal(journal_path)
    trial_rows = [record["trial"] for record in journal_records]
    system_rows = [record["system"] for record in journal_records]
    robot_rows = [row for record in journal_records for row in record["robots"]]
    target_rows = [row for record in journal_records for row in record["targets"]]
    for row in system_rows:
        row.setdefault("blocked_task_quarantines_total", 0)
        row.setdefault("blocked_task_quarantine_time_s_total", 0.0)
        row.setdefault("maximum_quarantine_level", 0)
    for row in robot_rows:
        row.setdefault("blocked_task_quarantines", 0)
        row.setdefault("blocked_task_quarantine_time_s", 0.0)
        row.setdefault("maximum_quarantine_level", 0)
    completed_trial_ids = {int(record["trial"]["trial_id"]) for record in journal_records}
    if completed_trial_ids:
        print(f"resuming with {len(completed_trial_ids)}/{len(scenarios)} trials checkpointed", flush=True)

    for idx, scenario in enumerate(scenarios):
        trial_id = int(getattr(scenario, "trial_id", idx))
        if trial_id in completed_trial_ids:
            continue
        model = make_comm_model(args.comm_model, args.comm_level)
        trial_seed = int(args.seed) + trial_id
        state = AsyncTrialRunner(cfg, allocator_cls, model, trial_seed).run_trial(scenario)
        trial, system, robots, targets = build_rows(
            state,
            args.algorithm,
            args.comm_model,
            comm_level_label,
            str(scenario_path),
        )
        meta = {
            "trial_mode": "known_visit",
            "benchmark_suite": args.suite_name,
            "condition_id": args.condition_id,
            "num_targets": args.num_targets,
            "num_robots": args.num_robots,
            "commitment_horizon": args.commitment_horizon,
        }
        if args.study_type == "horizon":
            meta.update({
                "sensitivity_suite": args.suite_name,
                "sensitivity_parameter": "commitment_horizon",
                "sensitivity_value": args.commitment_horizon,
                "sensitivity_label": f"h{args.commitment_horizon}",
            })
        for row in [trial, system, *robots, *targets]:
            row.update(meta)
        trial_rows.append(trial)
        system_rows.append(system)
        robot_rows.extend(robots)
        target_rows.extend(targets)
        record = {"trial": trial, "system": system, "robots": robots, "targets": targets}
        with journal_path.open("a", encoding="utf-8") as journal:
            journal.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
        completed_trial_ids.add(trial_id)
        print(f"completed {len(completed_trial_ids)}/{len(scenarios)} trials for {args.algorithm} h{args.commitment_horizon} {args.comm_model} {comm_level_label}", flush=True)

    extra_config = {
        "trial_mode": "known_visit",
        "study_type": args.study_type,
        "benchmark_suite": args.suite_name,
        "algorithm": args.algorithm,
        "comm_model": args.comm_model,
        "comm_level": comm_level_label,
        "commitment_horizon": args.commitment_horizon,
        "horizon_fields_changed": changed,
        "scenario_file": str(scenario_path),
        "num_trials_requested": args.num_trials,
        "max_trials": args.max_trials,
        "grid_size": args.grid_size,
        "num_robots": args.num_robots,
        "num_targets": args.num_targets,
        "seed": args.seed,
        "condition_id": args.condition_id,
        "checkpoint_journal": str(journal_path),
        "debug_max_events": args.debug_max_events,
        "debug_max_stagnant_events": args.debug_max_stagnant_events,
        "stalled_allocation_recovery_s": args.stalled_allocation_recovery_s,
    }
    write_outputs(out_dir, trial_rows, system_rows, robot_rows, target_rows, extra_config)
    (out_dir / "_COMPLETE.txt").write_text("complete\n", encoding="utf-8")


if __name__ == "__main__":
    main()
