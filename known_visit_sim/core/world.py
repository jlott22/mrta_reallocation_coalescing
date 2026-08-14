from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

from .reallocation import TaskState, TaskStateEvent, task_id_for_index, task_ids_for_scenario
from .types import Cell, TrialScenario


@dataclass
class VisitRecord:
    total_visits: int = 0
    by_robot: Dict[str, int] = field(default_factory=dict)


@dataclass
class TargetRecord:
    index: int
    cell: Cell
    task_id: Optional[Any] = None
    release_time_s: float = 0.0
    released_time_s: Optional[float] = 0.0
    pending_time_s: Optional[float] = 0.0
    admission_time_s: Optional[float] = 0.0
    admission_epoch_id: Optional[int] = None
    admission_trigger: Optional[str] = "initial_allocation"
    terminal_residual: bool = False
    knowledge_receipt_time_s_by_robot: Dict[str, float] = field(default_factory=dict)
    first_eligible_allocator_start_time_s: Optional[float] = None
    first_eligible_robot: Optional[str] = None
    first_assignment_time_s: Optional[float] = None
    first_assigned_robot: Optional[str] = None
    first_current_goal_time_s: Optional[float] = None
    first_current_goal_robot: Optional[str] = None
    current_goal_events: int = 0
    first_completion_time_s: Optional[float] = None
    first_found_by: Optional[str] = None
    completion_mode: Optional[str] = None
    assignment_events: int = 0
    total_visits: int = 0
    state: TaskState = TaskState.ADMITTED
    state_history: List[TaskStateEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.task_id is None:
            self.task_id = task_id_for_index(self.index)
        if not self.state_history and self.admission_time_s is not None:
            self.state_history = [
                TaskStateEvent(TaskState.RELEASED, 0.0),
                TaskStateEvent(TaskState.PENDING, 0.0),
                TaskStateEvent(TaskState.ADMITTED, 0.0),
            ]

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
        task_ids = task_ids_for_scenario(self.scenario)
        self.target_records = {
            cell: TargetRecord(index=index, cell=cell, task_id=task_ids[index - 1])
            for index, cell in enumerate(self.scenario.targets, start=1)
        }

    @property
    def targets(self) -> list[Cell]:
        return list(self.scenario.targets)

    @property
    def completed_targets(self) -> Set[Cell]:
        return {cell for cell, record in self.target_records.items() if record.completed}

    @property
    def admitted_targets(self) -> Set[Cell]:
        return {
            cell for cell, record in self.target_records.items()
            if record.admission_time_s is not None and not record.completed
        }

    def configure_release_times(self, release_times: Mapping[Cell, float]) -> None:
        if set(release_times) != set(self.target_records):
            raise ValueError("release trace must cover every scenario target exactly once")
        for cell, record in self.target_records.items():
            release_s = float(release_times[cell])
            record.release_time_s = release_s
            record.first_assignment_time_s = None
            record.first_assigned_robot = None
            record.first_current_goal_time_s = None
            record.first_current_goal_robot = None
            record.current_goal_events = 0
            record.admission_epoch_id = None
            record.admission_trigger = None
            record.terminal_residual = False
            record.knowledge_receipt_time_s_by_robot = {}
            record.first_eligible_allocator_start_time_s = None
            record.first_eligible_robot = None
            record.first_completion_time_s = None
            record.first_found_by = None
            record.completion_mode = None
            record.assignment_events = 0
            record.total_visits = 0
            if release_s <= 0.0:
                record.released_time_s = 0.0
                record.pending_time_s = 0.0
                record.admission_time_s = 0.0
                record.admission_trigger = "initial_allocation"
                record.state = TaskState.ADMITTED
                record.state_history = [
                    TaskStateEvent(TaskState.RELEASED, 0.0),
                    TaskStateEvent(TaskState.PENDING, 0.0),
                    TaskStateEvent(TaskState.ADMITTED, 0.0),
                ]
            else:
                record.released_time_s = None
                record.pending_time_s = None
                record.admission_time_s = None
                record.state = TaskState.UNRELEASED
                record.state_history = []

    def release_task(self, cell: Cell, time_s: float) -> TargetRecord:
        record = self.target_records[cell]
        if record.state != TaskState.UNRELEASED:
            raise RuntimeError(f"task {record.task_id} was released more than once")
        if abs(float(time_s) - record.release_time_s) > 1e-9:
            raise ValueError("release event must occur on its predetermined absolute timestamp")
        record.released_time_s = float(time_s)
        record.pending_time_s = float(time_s)
        record.state_history.append(TaskStateEvent(TaskState.RELEASED, float(time_s)))
        record.state = TaskState.RELEASED
        record.state_history.append(TaskStateEvent(TaskState.PENDING, float(time_s)))
        record.state = TaskState.PENDING
        return record

    def admit_task(self, cell: Cell, time_s: float) -> TargetRecord:
        record = self.target_records[cell]
        if record.state != TaskState.PENDING or record.released_time_s is None:
            raise RuntimeError(f"task {record.task_id} must be pending before admission")
        if float(time_s) + 1e-12 < record.released_time_s:
            raise ValueError("task admission cannot precede release")
        record.admission_time_s = float(time_s)
        record.state = TaskState.ADMITTED
        record.state_history.append(TaskStateEvent(TaskState.ADMITTED, float(time_s)))
        return record

    def record_admission_metadata(
        self,
        cell: Cell,
        epoch_id: int,
        trigger_reason: str,
        terminal_residual: bool = False,
    ) -> None:
        record = self.target_records[cell]
        record.admission_epoch_id = int(epoch_id)
        record.admission_trigger = str(trigger_reason)
        record.terminal_residual = bool(terminal_residual)

    def record_task_knowledge(
        self,
        rid: str,
        cells: Iterable[Cell],
        time_s: float,
        epoch_id: int,
    ) -> None:
        """Record message receipt without making it allocator-visible."""

        for cell in cells:
            record = self.target_records.get(cell)
            if record is None or record.admission_time_s is None:
                continue
            if record.admission_epoch_id not in (None, int(epoch_id)):
                raise RuntimeError("task knowledge receipt references the wrong epoch")
            if float(time_s) + 1e-12 < record.admission_time_s:
                raise RuntimeError("task knowledge cannot precede admission")
            record.knowledge_receipt_time_s_by_robot.setdefault(
                str(rid), float(time_s)
            )

    def record_assignment(self, rid: str, cells: Iterable[Cell], time_s: float) -> None:
        for cell in cells:
            record = self.target_records.get(cell)
            if record is None:
                continue
            if record.admission_time_s is None or float(time_s) + 1e-12 < record.admission_time_s:
                raise RuntimeError(f"task {record.task_id} assigned before admission")
            if record.completed:
                continue
            record.assignment_events += 1
            if record.first_assignment_time_s is None:
                record.first_assignment_time_s = float(time_s)
                record.first_assigned_robot = str(rid)
                record.state = TaskState.ASSIGNED
                record.state_history.append(
                    TaskStateEvent(TaskState.ASSIGNED, float(time_s), str(rid))
                )

    def record_current_goal(self, rid: str, cell: Cell, time_s: float) -> None:
        """Record execution-head selection separately from bundle ownership."""

        record = self.target_records.get(cell)
        if record is None or record.admission_time_s is None or record.completed:
            return
        receipt = record.knowledge_receipt_time_s_by_robot.get(str(rid))
        if receipt is not None and float(time_s) + 1e-12 < receipt:
            raise RuntimeError("robot selected a task before learning it")
        record.current_goal_events += 1
        if record.first_current_goal_time_s is None:
            record.first_current_goal_time_s = float(time_s)
            record.first_current_goal_robot = str(rid)

    def record_allocator_start(
        self, rid: str, cells: Iterable[Cell], time_s: float
    ) -> None:
        """Record the first safe-boundary call that can observe each task."""

        for cell in cells:
            record = self.target_records.get(cell)
            if (
                record is None
                or record.admission_time_s is None
                or record.completed
                or float(time_s) + 1e-12 < record.admission_time_s
            ):
                continue
            if record.first_eligible_allocator_start_time_s is None:
                record.first_eligible_allocator_start_time_s = float(time_s)
                record.first_eligible_robot = str(rid)

    def record_visit(self, rid: str, cell: Cell) -> bool:
        record = self.visits.setdefault(cell, VisitRecord())
        revisited = record.total_visits > 0
        record.total_visits += 1
        record.by_robot[rid] = record.by_robot.get(rid, 0) + 1
        return revisited

    def record_target_visit(
        self,
        rid: str,
        cell: Cell,
        time_s: float,
        completion_mode: str = "movement_arrival",
    ) -> tuple[bool, bool]:
        record = self.target_records.get(cell)
        if (
            record is None
            or record.admission_time_s is None
            or float(time_s) + 1e-12 < record.admission_time_s
        ):
            return False, False
        record.total_visits += 1
        first_completion = not record.completed
        if first_completion:
            # A robot may physically traverse an admitted task cell that was
            # absent from its allocator-owned bundle.  Treat service by that
            # robot as an assignment at the same timestamp, immediately before
            # completion, so lifecycle ordering remains total and explicit.
            if record.first_assignment_time_s is None:
                record.assignment_events += 1
                record.first_assignment_time_s = float(time_s)
                record.first_assigned_robot = str(rid)
                record.state = TaskState.ASSIGNED
                record.state_history.append(
                    TaskStateEvent(TaskState.ASSIGNED, float(time_s), str(rid))
                )
            record.first_completion_time_s = time_s
            record.first_found_by = rid
            record.completion_mode = str(completion_mode)
            record.state = TaskState.COMPLETED
            record.state_history.append(TaskStateEvent(TaskState.COMPLETED, float(time_s), str(rid)))
        return True, first_completion

    def task_rows(self, trial_id: Optional[int] = None) -> list[dict]:
        rows = []
        for record in sorted(self.target_records.values(), key=lambda item: item.index):
            release = record.released_time_s
            admission = record.admission_time_s
            assignment = record.first_assignment_time_s
            completion = record.first_completion_time_s
            eligible = record.first_eligible_allocator_start_time_s
            rows.append({
                "trial_id": self.scenario.trial_id if trial_id is None else trial_id,
                "task_id": record.task_id,
                "task_index": record.index,
                "task_x": record.cell[0],
                "task_y": record.cell[1],
                "x": record.cell[0],
                "y": record.cell[1],
                "state": record.state.value,
                "release_time_s": release,
                "pending_time_s": record.pending_time_s,
                "admission_time_s": admission,
                "admission_epoch_id": record.admission_epoch_id,
                "admission_trigger": record.admission_trigger,
                "terminal_residual": record.terminal_residual,
                "knowledge_receipt_time_s_by_robot": dict(
                    record.knowledge_receipt_time_s_by_robot
                ),
                "first_knowledge_receipt_time_s": min(
                    record.knowledge_receipt_time_s_by_robot.values(),
                    default=None,
                ),
                "first_eligible_allocator_start_time_s": eligible,
                "first_eligible_processing_time_s": eligible,
                "first_eligible_robot": record.first_eligible_robot,
                "first_assignment_time_s": assignment,
                "first_assigned_robot": record.first_assigned_robot,
                "first_current_goal_time_s": record.first_current_goal_time_s,
                "first_current_goal_robot": record.first_current_goal_robot,
                "current_goal_events": record.current_goal_events,
                "completion_time_s": completion,
                "completing_robot": record.first_found_by,
                "completion_mode": record.completion_mode,
                "assignment_events": record.assignment_events,
                "reassignment_count": max(0, record.assignment_events - 1),
                "release_to_admission_latency_s": (
                    admission - release if admission is not None and release is not None else None
                ),
                "release_to_first_assignment_latency_s": (
                    assignment - release if assignment is not None and release is not None else None
                ),
                "admission_to_first_assignment_latency_s": (
                    assignment - admission if assignment is not None and admission is not None else None
                ),
                "admission_to_first_current_goal_latency_s": (
                    record.first_current_goal_time_s - admission
                    if record.first_current_goal_time_s is not None
                    and admission is not None else None
                ),
                "admission_to_first_eligible_allocator_start_latency_s": (
                    eligible - admission if eligible is not None and admission is not None else None
                ),
                "release_to_completion_latency_s": (
                    completion - release if completion is not None and release is not None else None
                ),
                "assignment_to_completion_latency_s": (
                    completion - assignment if completion is not None and assignment is not None else None
                ),
                "state_history": [
                    {
                        "state": event.state.value,
                        "time_s": event.time_s,
                        "robot_id": event.robot_id,
                    }
                    for event in record.state_history
                ],
            })
        return rows

    def all_targets_completed(self) -> bool:
        return bool(self.target_records) and all(record.completed for record in self.target_records.values())

    def unique_cells_searched(self) -> int:
        return len(self.visits)

    def system_revisits(self) -> int:
        return sum(max(0, record.total_visits - 1) for record in self.visits.values())
