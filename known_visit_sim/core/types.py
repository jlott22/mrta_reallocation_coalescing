from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

Cell = Tuple[int, int]
Heading = Tuple[int, int]
DIRS4: Tuple[Heading, ...] = ((0, 1), (1, 0), (0, -1), (-1, 0))
NORTH, EAST, SOUTH, WEST = DIRS4


def manhattan(a: Cell, b: Cell) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def in_bounds(cell: Cell, grid_size: int) -> bool:
    return 0 <= cell[0] < grid_size and 0 <= cell[1] < grid_size


def quarter_turns(from_dir: Optional[Heading], to_dir: Heading) -> int:
    if from_dir == to_dir:
        return 0
    if from_dir is None:
        return 1
    try:
        fi, ti = DIRS4.index(from_dir), DIRS4.index(to_dir)
    except ValueError:
        return 1
    delta = (ti - fi) % 4
    return 2 if delta == 2 else 1


@dataclass(frozen=True)
class TrialScenario:
    trial_id: int
    targets: List[Cell]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Observation:
    time_s: float
    cell: Cell
    searched: bool = True
    target_visited: bool = False
    first_completion: bool = False


@dataclass
class AllocationDecision:
    goal: Optional[Cell]
    debug: Dict[str, Any] = field(default_factory=dict)
