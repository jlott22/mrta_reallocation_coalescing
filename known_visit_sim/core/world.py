from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Set

from .types import Cell, TrialScenario


@dataclass
class VisitRecord:
    total_visits: int = 0
    by_robot: Dict[str, int] = field(default_factory=dict)


@dataclass
class TargetRecord:
    index: int
    cell: Cell
    first_completion_time_s: Optional[float] = None
    first_found_by: Optional[str] = None
    total_visits: int = 0

    @property
    def completed(self) -> bool:
        return self.first_completion_time_s is not None

    @property
    def duplicate_visits(self) -> int:
        return max(0, self.total_visits - 1)


@dataclass
class World:
    grid_size: int
    scenario: TrialScenario
    visits: Dict[Cell, VisitRecord] = field(default_factory=dict)
    target_records: Dict[Cell, TargetRecord] = field(init=False)

    def __post_init__(self) -> None:
        self.target_records = {
            cell: TargetRecord(index=index, cell=cell)
            for index, cell in enumerate(self.scenario.targets, start=1)
        }

    @property
    def targets(self) -> list[Cell]:
        return list(self.scenario.targets)

    @property
    def completed_targets(self) -> Set[Cell]:
        return {cell for cell, record in self.target_records.items() if record.completed}

    def record_visit(self, rid: str, cell: Cell) -> bool:
        record = self.visits.setdefault(cell, VisitRecord())
        revisited = record.total_visits > 0
        record.total_visits += 1
        record.by_robot[rid] = record.by_robot.get(rid, 0) + 1
        return revisited

    def record_target_visit(self, rid: str, cell: Cell, time_s: float) -> tuple[bool, bool]:
        record = self.target_records.get(cell)
        if record is None:
            return False, False
        record.total_visits += 1
        first_completion = not record.completed
        if first_completion:
            record.first_completion_time_s = time_s
            record.first_found_by = rid
        return True, first_completion

    def all_targets_completed(self) -> bool:
        return bool(self.target_records) and all(record.completed for record in self.target_records.values())

    def unique_cells_searched(self) -> int:
        return len(self.visits)

    def system_revisits(self) -> int:
        return sum(max(0, record.total_visits - 1) for record in self.visits.values())
