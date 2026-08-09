from __future__ import annotations

import argparse
import csv
from pathlib import Path

from known_visit_sim.algorithms.registry import load_allocator_class
from known_visit_sim.comms.models import make_comm_model
from known_visit_sim.config import EAST, SimConfig, edge_even_start_positions, generate_robot_ids
from known_visit_sim.core.scenario_loader import load_scenarios
from known_visit_sim.core.scheduler import AsyncTrialRunner
from known_visit_sim.metrics.export import write_outputs
from known_visit_sim.metrics.summary import build_rows


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def candidate_limit(value: str) -> int | None:
    return None if value.lower() == "all" else positive_int(value)


def read_existing_csv(path: str | Path) -> list[dict]:
    csv_path = Path(path)
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return []
    with csv_path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def parse_trial_id(row: dict) -> int | None:
    value = row.get("trial_id")
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def recorded_trial_ids(*row_groups: list[dict]) -> set[int]:
    recorded: set[int] = set()
    for rows in row_groups:
        for row in rows:
            trial_id = parse_trial_id(row)
            if trial_id is None:
                continue
            status = str(row.get("trial_status", "")).strip().lower()
            if status in {"", "completed", "failed"}:
                recorded.add(trial_id)
    return recorded


def load_existing_outputs(out_dir: str | Path) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    out = Path(out_dir)
    return (
        read_existing_csv(out / "trial_summary.csv"),
        read_existing_csv(out / "system_performance.csv"),
        read_existing_csv(out / "robot_performance.csv"),
        read_existing_csv(out / "target_performance.csv"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run static collaborative known-target visits.")
    parser.add_argument("--scenario-file", required=True)
    parser.add_argument("--algorithm", required=True)
    parser.add_argument("--algorithm-name", default=None)
    parser.add_argument(
        "--comm-model",
        default="ideal",
        choices=["ideal", "bernoulli", "gilbert_elliott", "gilbert_elliot", "rayleigh_style"],
        help="Communication model; gilbert_elliot is accepted as a legacy alias.",
    )
    parser.add_argument("--comm-level", type=float, default=None)
    parser.add_argument("--max-trials", type=int, default=None)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out-dir", default="results/known_target_visit/default")
    parser.add_argument("--grid-size", type=positive_int, default=19)
    parser.add_argument("--num-robots", type=positive_int, default=4)
    parser.add_argument("--robot-start-layout", choices=["edge_even"], default="edge_even")
    parser.add_argument("--condition-id", default="")
    parser.add_argument("--commitment-horizon", type=positive_int, default=None)
    parser.add_argument("--max-candidate-cells", type=candidate_limit, default=None)
    return parser.parse_args()


def failure_rows(scenario, args, cfg, algorithm: str, comm_level: str, exc: BaseException):
    error_type = type(exc).__name__
    error_message = str(exc)
    targets = list(getattr(scenario, "targets", []) or [])
    common = {
        "trial_id": scenario.trial_id,
        "algorithm": algorithm,
        "comm_model": args.comm_model,
        "comm_level": comm_level,
        "grid_size": cfg.grid_size,
        "grid_cells": cfg.grid_size * cfg.grid_size,
        "robot_count": len(cfg.robot_ids),
        "condition_id": cfg.condition_id,
        "scenario_file": args.scenario_file,
        "trial_status": "failed",
        "failure_type": error_type,
        "failure_message": error_message,
    }
    trial = {
        **common,
        "target_count": len(targets),
        "completed_target_count": "",
        "target_locations": ";".join(f"({x},{y})" for x, y in targets),
        "robot_start_locations": ";".join(
            f"{rid}:({cfg.start_positions[rid][0]},{cfg.start_positions[rid][1]})"
            for rid in cfg.robot_ids
        ),
    }
    system = {
        **common,
        "target_count": len(targets),
        "completed_target_count": "",
        "total_team_steps": "",
        "unique_targets_completed": "",
        "debug_max_events": cfg.debug_max_events,
        "allocator_calls_total": "",
        "allocator_time_ms_team_total": "",
        "allocator_time_ms_team_max": "",
        "allocator_solve_time_ms_team_total": "",
        "allocator_solve_time_ms_team_max": "",
        "candidate_filter_calls_total": "",
        "candidate_filter_time_ms_team_total": "",
        "candidate_filter_time_ms_team_max": "",
    }
    robots = [
        {
            **common,
            "robot_id": rid,
            "steps_total": "",
            "targets_completed": "",
            "messages_sent": "",
            "messages_delivered_to_robot": "",
            "messages_dropped_to_robot": "",
            "allocator_calls": "",
            "allocator_time_ms_total": "",
            "allocator_time_ms_mean": "",
            "allocator_time_ms_median": "",
            "allocator_time_ms_p95": "",
            "allocator_time_ms_max": "",
            "allocator_solve_time_ms_total": "",
            "allocator_solve_time_ms_mean": "",
            "allocator_solve_time_ms_median": "",
            "allocator_solve_time_ms_p95": "",
            "allocator_solve_time_ms_max": "",
            "candidate_filter_calls": "",
            "candidate_filter_time_ms_total": "",
            "candidate_filter_time_ms_mean": "",
            "candidate_filter_time_ms_median": "",
            "candidate_filter_time_ms_p95": "",
            "candidate_filter_time_ms_max": "",
        }
        for rid in cfg.robot_ids
    ]
    target_rows = [
        {
            **common,
            "target_index": index,
            "target_x": target[0],
            "target_y": target[1],
            "completed": "",
            "completed_by_robot": "",
            "completion_time_s": "",
        }
        for index, target in enumerate(targets)
    ]
    return trial, system, robots, target_rows


def main() -> None:
    args = parse_args()
    robot_ids = generate_robot_ids(args.num_robots)
    starts = edge_even_start_positions(args.grid_size, robot_ids)
    cfg = SimConfig(
        grid_size=args.grid_size,
        robot_ids=robot_ids,
        start_positions=starts,
        start_headings={rid: EAST for rid in robot_ids},
        robot_start_layout=args.robot_start_layout,
        condition_id=args.condition_id,
        commitment_horizon=args.commitment_horizon,
        max_candidate_cells=args.max_candidate_cells,
    )
    scenarios = load_scenarios(
        args.scenario_file, args.grid_size, set(starts.values()), args.max_trials
    )
    allocator_cls = load_allocator_class(args.algorithm)
    algorithm = args.algorithm_name or getattr(allocator_cls, "name", allocator_cls.__name__)
    comm = make_comm_model(args.comm_model, args.comm_level)
    args.comm_model = comm.name
    comm_level = comm.level_label()
    output_config = {
        "sim_config": cfg.to_dict(),
        "algorithm": args.algorithm,
        "algorithm_name": algorithm,
        "comm_model": args.comm_model,
        "comm_level": comm_level,
        "scenario_file": args.scenario_file,
        "seed": args.seed,
    }
    trial_rows, system_rows, robot_rows, target_rows = load_existing_outputs(args.out_dir)
    done_trial_ids = recorded_trial_ids(trial_rows, system_rows)
    total_scenarios = len(scenarios)
    scenario_ids = {scenario.trial_id for scenario in scenarios}
    resumed_count = len(done_trial_ids & scenario_ids)
    if resumed_count:
        print(
            f"resuming {args.out_dir}: {resumed_count}/{total_scenarios} trials already recorded",
            flush=True,
        )
    for scenario in scenarios:
        if scenario.trial_id in done_trial_ids:
            print(f"skipping recorded trial {scenario.trial_id}", flush=True)
            continue
        try:
            state = AsyncTrialRunner(
                cfg, allocator_cls, make_comm_model(args.comm_model, args.comm_level),
                args.seed + scenario.trial_id * 1009,
            ).run_trial(scenario)
            trial, system, robots, targets = build_rows(
                state, algorithm, args.comm_model, comm_level, str(Path(args.scenario_file))
            )
            trial["trial_status"] = "completed"
            system["trial_status"] = "completed"
            for row in robots:
                row["trial_status"] = "completed"
            for row in targets:
                row["trial_status"] = "completed"
        except Exception as exc:
            trial, system, robots, targets = failure_rows(scenario, args, cfg, algorithm, comm_level, exc)
        trial_rows.append(trial)
        system_rows.append(system)
        robot_rows.extend(robots)
        target_rows.extend(targets)
        done_trial_ids.add(scenario.trial_id)
        write_outputs(args.out_dir, trial_rows, system_rows, robot_rows, target_rows, output_config)
        if trial.get("trial_status") == "failed":
            print(
                f"failed trial {scenario.trial_id}: {trial['failure_type']}: {trial['failure_message']}",
                flush=True,
            )
        else:
            print(
                f"completed trial {scenario.trial_id}: "
                f"targets={system['completed_target_count']}/{system['target_count']} "
                f"steps={system['total_team_steps']}",
                flush=True,
            )
    write_outputs(
        args.out_dir, trial_rows, system_rows, robot_rows, target_rows,
        output_config,
    )
    print(f"outputs written to {args.out_dir}")


if __name__ == "__main__":
    main()
