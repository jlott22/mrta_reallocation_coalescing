from __future__ import annotations

import argparse
import csv
import random
from pathlib import Path

from known_visit_sim.config import edge_even_start_positions, generate_robot_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate independent static known-target scenarios.")
    parser.add_argument("--grid-size", type=int, default=19)
    parser.add_argument("--num-trials", type=int, default=500)
    parser.add_argument("--num-targets", type=int, default=10)
    parser.add_argument("--num-robots", type=int, default=4)
    parser.add_argument("--robot-start-layout", choices=["edge_even"], default="edge_even")
    parser.add_argument("--seed", type=int, default=20260630)
    parser.add_argument("--output", type=Path, default=Path("scenarios/known_visit_g19_t10_n500.csv"))
    return parser.parse_args()


def generate(grid_size: int, num_trials: int, num_targets: int,
             num_robots: int, seed: int) -> list[list[tuple[int, int]]]:
    if grid_size <= 0 or num_trials <= 0 or num_targets <= 0 or num_robots <= 0:
        raise ValueError("grid size, trial count, target count, and robot count must be positive")
    starts = set(edge_even_start_positions(grid_size, generate_robot_ids(num_robots)).values())
    eligible = [
        (x, y) for y in range(grid_size) for x in range(grid_size)
        if (x, y) not in starts
    ]
    if num_targets > len(eligible):
        raise ValueError("num_targets exceeds eligible non-start grid cells")
    rng = random.Random(seed)
    return [rng.sample(eligible, num_targets) for _ in range(num_trials)]


def main() -> None:
    args = parse_args()
    scenarios = generate(
        args.grid_size, args.num_trials, args.num_targets, args.num_robots, args.seed
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    header = ["trial_id"]
    for index in range(1, args.num_targets + 1):
        header.extend([f"target{index}_x", f"target{index}_y"])
    with args.output.open("w", newline="") as handle:
        handle.write(
            f"# grid_size={args.grid_size}, num_targets={args.num_targets}, "
            f"num_robots={args.num_robots}, layout={args.robot_start_layout}, seed={args.seed}\n"
        )
        writer = csv.writer(handle)
        writer.writerow(header)
        for trial_id, targets in enumerate(scenarios):
            writer.writerow([trial_id, *[value for cell in targets for value in cell]])
    print(f"wrote {len(scenarios)} paired scenarios to {args.output}")


if __name__ == "__main__":
    main()
