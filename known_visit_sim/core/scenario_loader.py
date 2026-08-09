from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import List, Optional

from .types import Cell, TrialScenario, in_bounds


def load_scenarios(
    path: str | Path,
    grid_size: int,
    start_cells: set[Cell],
    max_trials: Optional[int] = None,
) -> List[TrialScenario]:
    path = Path(path)
    scenarios = _load_json(path, max_trials) if path.suffix.lower() == ".json" else _load_csv(path, max_trials)
    for scenario in scenarios:
        if not scenario.targets:
            raise ValueError(f"Trial {scenario.trial_id} has no targets")
        if len(set(scenario.targets)) != len(scenario.targets):
            raise ValueError(f"Trial {scenario.trial_id} contains duplicate targets")
        for cell in scenario.targets:
            if not in_bounds(cell, grid_size):
                raise ValueError(f"Trial {scenario.trial_id} target {cell} is out of bounds")
            if cell in start_cells:
                raise ValueError(f"Trial {scenario.trial_id} target {cell} overlaps a robot start")
    return scenarios


def _load_csv(path: Path, max_trials: Optional[int]) -> List[TrialScenario]:
    lines = [line for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    scenarios: List[TrialScenario] = []
    for index, row in enumerate(reader):
        if max_trials is not None and len(scenarios) >= max_trials:
            break
        targets: List[Cell] = []
        target_index = 1
        while row.get(f"target{target_index}_x", "") != "":
            targets.append((int(row[f"target{target_index}_x"]), int(row[f"target{target_index}_y"])))
            target_index += 1
        scenarios.append(TrialScenario(
            trial_id=int(row.get("trial_id", row.get("episode", index))),
            targets=targets,
            metadata={"scenario_file": str(path)},
        ))
    return scenarios


def _load_json(path: Path, max_trials: Optional[int]) -> List[TrialScenario]:
    scenarios: List[TrialScenario] = []
    for index, item in enumerate(json.loads(path.read_text())):
        if max_trials is not None and len(scenarios) >= max_trials:
            break
        scenarios.append(TrialScenario(
            trial_id=int(item.get("trial_id", item.get("episode", index))),
            targets=[(int(cell[0]), int(cell[1])) for cell in item.get("targets", [])],
            metadata={**item.get("metadata", {}), "scenario_file": str(path)},
        ))
    return scenarios
