from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from math import ceil, isfinite
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, TYPE_CHECKING, Union

from .types import Cell, TrialScenario

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .robot import RobotShell
    from .scheduler import TrialState
    from .world import World


class TaskState(str, Enum):
    """Observable states in an online task's lifecycle.

    RELEASED is intentionally represented even though a release is immediately
    placed in PENDING.  The lifecycle event history therefore preserves both
    transitions at the same deterministic absolute timestamp.
    """

    UNRELEASED = "unreleased"
    RELEASED = "released"
    PENDING = "pending"
    ADMITTED = "admitted"
    ASSIGNED = "assigned"
    COMPLETED = "completed"


@dataclass(frozen=True)
class TaskStateEvent:
    state: TaskState
    time_s: float
    robot_id: Optional[str] = None


@dataclass(frozen=True)
class ReallocationPolicy:
    """Allocator-independent admission policy for online task arrivals."""

    mode: str = "eager"
    batch_size: int = 1
    max_pending_age_s: Optional[float] = None

    def __post_init__(self) -> None:
        mode = str(self.mode).strip().lower()
        if mode not in {"eager", "count", "bounded"}:
            raise ValueError("policy mode must be eager, count, or bounded")
        if isinstance(self.batch_size, bool) or int(self.batch_size) <= 0:
            raise ValueError("batch_size must be positive")
        if mode == "eager" and int(self.batch_size) != 1:
            raise ValueError("eager policy requires batch_size=1")
        if mode == "bounded":
            if (
                self.max_pending_age_s is None
                or not isfinite(float(self.max_pending_age_s))
                or float(self.max_pending_age_s) <= 0
            ):
                raise ValueError("bounded policy requires a positive max_pending_age_s")
        elif self.max_pending_age_s is not None:
            raise ValueError("max_pending_age_s is only valid for bounded policy")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "batch_size", int(self.batch_size))
        if self.max_pending_age_s is not None:
            object.__setattr__(self, "max_pending_age_s", float(self.max_pending_age_s))

    @classmethod
    def eager(cls) -> "ReallocationPolicy":
        return cls("eager", 1)

    @classmethod
    def count(cls, batch_size: int) -> "ReallocationPolicy":
        return cls("count", batch_size)

    @classmethod
    def bounded(cls, batch_size: int, max_pending_age_s: float) -> "ReallocationPolicy":
        return cls("bounded", batch_size, max_pending_age_s)

    @classmethod
    def from_spec(cls, spec: Union["ReallocationPolicy", str, Mapping[str, Any]]) -> "ReallocationPolicy":
        if isinstance(spec, cls):
            return spec
        if isinstance(spec, str):
            value = spec.strip().lower()
            if value == "eager":
                return cls.eager()
            if value.startswith("count-"):
                return cls.count(int(value.split("-", 1)[1]))
            raise ValueError(f"unsupported policy specification: {spec!r}")
        if isinstance(spec, Mapping):
            mode = str(spec.get("mode", spec.get("policy", "eager"))).lower()
            batch_size = int(spec.get("batch_size", spec.get("B", 1)))
            wait = spec.get("max_pending_age_s", spec.get("W"))
            return cls(mode, batch_size, None if wait is None else float(wait))
        raise TypeError("policy must be ReallocationPolicy, string, or mapping")

    def to_dict(self) -> dict:
        row = asdict(self)
        row["B"] = self.batch_size
        row["W"] = self.max_pending_age_s
        return row

    @property
    def B(self) -> int:
        return self.batch_size

    @property
    def W(self) -> Optional[float]:
        return self.max_pending_age_s


ReleaseTimes = Union[Sequence[float], Mapping[Any, float]]


def task_id_for_index(index: int) -> str:
    return f"task_{index:04d}"


def task_ids_for_scenario(scenario: TrialScenario) -> List[Any]:
    raw = scenario.metadata.get("task_ids")
    if raw is None:
        return [task_id_for_index(index) for index in range(1, len(scenario.targets) + 1)]
    if isinstance(raw, (str, bytes)):
        raise ValueError("scenario metadata task_ids must be a sequence")
    try:
        task_ids = list(raw)
    except TypeError as exc:
        raise ValueError("scenario metadata task_ids must be a sequence") from exc
    if len(task_ids) != len(scenario.targets):
        raise ValueError("scenario metadata task_ids length must equal target count")
    try:
        unique_count = len(set(task_ids))
    except TypeError as exc:
        raise ValueError("scenario metadata task_ids must be hashable") from exc
    if unique_count != len(task_ids):
        raise ValueError("scenario metadata task_ids must be unique")
    return task_ids


def normalize_release_times(scenario: TrialScenario, release_times: ReleaseTimes) -> Dict[Cell, float]:
    """Normalize a serializable trace without depending on RNG call order.

    A sequence is aligned to ``scenario.targets``.  A mapping may use target
    cells, target indices (one-based), their string forms, or ``task_0001`` IDs.
    Every target must appear exactly once.
    """

    targets = list(scenario.targets)
    task_ids = task_ids_for_scenario(scenario)
    if len(set(targets)) != len(targets):
        raise ValueError("scenario target cells must be unique")
    if isinstance(release_times, Mapping):
        normalized: Dict[Cell, float] = {}
        for index, cell in enumerate(targets, start=1):
            candidates = []
            for candidate in (cell, index, str(index), task_ids[index - 1]):
                if candidate not in candidates:
                    candidates.append(candidate)
            present = [key for key in candidates if key in release_times]
            if len(present) != 1:
                raise ValueError(f"release trace must contain exactly one time for {task_ids[index - 1]}")
            normalized[cell] = float(release_times[present[0]])
    else:
        values = list(release_times)
        if len(values) != len(targets):
            raise ValueError("release time sequence length must equal target count")
        normalized = {cell: float(value) for cell, value in zip(targets, values)}
    if any(not isfinite(value) or value < 0 for value in normalized.values()):
        raise ValueError("release times must be finite and non-negative")
    return normalized


@dataclass
class QueueSample:
    time_s: float
    depth: int
    oldest_age_s: float
    event: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AllocatorCallRecord:
    call_id: int
    robot_id: str
    mission_time_s: float
    duration_ns: int
    epoch_id: Optional[int] = None
    trigger_reason: str = "other"

    @property
    def duration_s(self) -> float:
        return self.duration_ns / 1_000_000_000.0

    def to_dict(self) -> dict:
        row = asdict(self)
        row["duration_s"] = self.duration_s
        return row


@dataclass
class AllocationEpoch:
    epoch_id: int
    opened_time_s: float
    trigger_reason: str
    mandatory: bool
    piggybacked_pending: bool
    pending_depth_before: int
    oldest_pending_age_s: float
    admitted_task_ids: List[Any] = field(default_factory=list)
    expected_robot_ids: List[str] = field(default_factory=list)
    allocator_call_ids: List[int] = field(default_factory=list)
    called_robot_ids: List[str] = field(default_factory=list)
    allocator_time_ns: int = 0
    closed_time_s: Optional[float] = None

    @property
    def admitted_count(self) -> int:
        return len(self.admitted_task_ids)

    def to_dict(self) -> dict:
        row = asdict(self)
        row["admitted_count"] = self.admitted_count
        row["allocator_time_s"] = self.allocator_time_ns / 1_000_000_000.0
        row["initial_call_phase_completed_time_s"] = self.closed_time_s
        row["closure_semantics"] = "first_expected_allocator_call_per_robot_not_consensus_convergence"
        return row


def allocator_parallel_critical_path_s(
    calls: Iterable[AllocatorCallRecord],
) -> float:
    """Estimate elapsed allocator delay for independent robot processors.

    Calls belonging to the same logical epoch at the same simulated timestamp
    are simultaneous across robots. Repeated calls by one robot in that group
    remain serial; the group contributes the maximum per-robot duration. Calls
    at different timestamps or in different epochs remain sequential.
    """

    groups: Dict[tuple[Optional[int], float], Dict[str, float]] = {}
    for call in calls:
        key = (call.epoch_id, round(float(call.mission_time_s), 12))
        per_robot = groups.setdefault(key, {})
        per_robot[call.robot_id] = per_robot.get(call.robot_id, 0.0) + call.duration_s
    return sum(max(per_robot.values(), default=0.0) for per_robot in groups.values())


ARRIVAL_REASONS = {
    "task_arrival_eager",
    "batch_threshold",
    "age_timeout",
    "final_release_flush",
}
MANDATORY_REASONS = {"task_completion", "invalid_goal", "robot_idle"}


class OnlineReallocationScheduler:
    """Global environment scheduler above otherwise unchanged allocators."""

    def __init__(self, world: "World", policy: ReallocationPolicy) -> None:
        self.world = world
        self.policy = ReallocationPolicy.from_spec(policy)
        self.robots: Dict[str, "RobotShell"] = {}
        self.pending_cells: List[Cell] = []
        self.epochs: List[AllocationEpoch] = []
        self.queue_samples: List[QueueSample] = []
        self.allocator_calls: List[AllocatorCallRecord] = []
        self._next_epoch_id = 1
        self._contexts: Dict[str, List[tuple[int, str]]] = {}
        self._active_epoch_by_robot: Dict[str, int] = {}

    def attach_robots(self, robots: Mapping[str, "RobotShell"]) -> None:
        self.robots = dict(robots)
        for robot in self.robots.values():
            robot.attach_reallocation_scheduler(self)
        initially_admitted = [
            record for record in self.world.target_records.values()
            if record.admission_time_s is not None
        ]
        if initially_admitted:
            epoch = self._open_epoch(
                0.0,
                "initial_allocation",
                mandatory=True,
                pending_depth=0,
                oldest_age_s=0.0,
                admitted_task_ids=[record.task_id for record in initially_admitted],
                expected_robot_ids=sorted(self.robots),
            )
            self._set_contexts(epoch, self.robots)

    @property
    def pending_count(self) -> int:
        return len(self.pending_cells)

    @property
    def unreleased_count(self) -> int:
        return sum(record.state == TaskState.UNRELEASED for record in self.world.target_records.values())

    def oldest_pending_age_s(self, now_s: float) -> float:
        release_times = [
            self.world.target_records[cell].released_time_s
            for cell in self.pending_cells
            if self.world.target_records[cell].released_time_s is not None
        ]
        return max(0.0, float(now_s) - min(release_times)) if release_times else 0.0

    def next_timeout_s(self) -> Optional[float]:
        if self.policy.mode != "bounded" or not self.pending_cells:
            return None
        releases = [
            self.world.target_records[cell].released_time_s
            for cell in self.pending_cells
            if self.world.target_records[cell].released_time_s is not None
        ]
        if not releases:
            return None
        return min(releases) + float(self.policy.max_pending_age_s)

    def release_due(self, now_s: float) -> Optional[AllocationEpoch]:
        now_s = float(now_s)
        due = [
            record for record in self.world.target_records.values()
            if record.state == TaskState.UNRELEASED and record.release_time_s <= now_s + 1e-12
        ]
        due.sort(key=lambda record: (record.release_time_s, record.index))
        for record in due:
            self.world.release_task(record.cell, record.release_time_s)
            self.pending_cells.append(record.cell)
        if due:
            self._sample(now_s, "release")
        if not due:
            return None
        if self.policy.mode == "eager":
            return self._admit_pending(now_s, "task_arrival_eager", mandatory=False)
        if self.pending_count >= self.policy.batch_size:
            return self._admit_pending(now_s, "batch_threshold", mandatory=False)
        # The final exogenous release is an explicit deterministic tail flush;
        # pure count batching therefore cannot strand fewer than B tasks.
        if self.unreleased_count == 0:
            return self._admit_pending(now_s, "final_release_flush", mandatory=False)
        return None

    def timeout_due(self, now_s: float) -> Optional[AllocationEpoch]:
        deadline = self.next_timeout_s()
        if deadline is None or float(now_s) + 1e-12 < deadline:
            return None
        return self._admit_pending(float(now_s), "age_timeout", mandatory=False)

    def mandatory_event(
        self,
        now_s: float,
        reason: str,
        source_robot_id: Optional[str] = None,
    ) -> AllocationEpoch:
        """Open one logical epoch for a genuine pre-existing mission event.

        Pending tasks are always piggybacked and therefore make the epoch
        global. A task completion is global even without pending work; a local
        invalid-goal event expects only its source robot.
        """

        if reason not in MANDATORY_REASONS:
            raise ValueError(f"unsupported mandatory trigger reason: {reason}")
        if self.pending_cells:
            return self._admit_pending(float(now_s), reason, mandatory=True)
        expected = (
            [str(source_robot_id)]
            if source_robot_id is not None
            else sorted(self.robots)
        )
        epoch = self._open_epoch(
            float(now_s), reason, mandatory=True,
            pending_depth=0, oldest_age_s=0.0,
            admitted_task_ids=[], expected_robot_ids=expected,
        )
        selected = {rid: self.robots[rid] for rid in expected if rid in self.robots}
        self._set_contexts(epoch, selected)
        if reason == "task_completion":
            # Peer consensus calls caused by this completion attach to the
            # same logical epoch, but only the newly idle source robot is a
            # mandatory first-round participant.
            for rid in self.robots:
                self._active_epoch_by_robot[rid] = epoch.epoch_id
        return epoch

    def before_allocator_call(
        self, robot: "RobotShell", now_s: float, reason: str
    ) -> tuple[Optional[int], str]:
        contexts = self._contexts.get(robot.rid, [])
        if contexts:
            context = contexts.pop(0)
            if not contexts:
                self._contexts.pop(robot.rid, None)
            self._active_epoch_by_robot[robot.rid] = context[0]
            return context
        # Pending arrivals may piggyback only on a genuine mandatory call.
        # Consensus/internal polling must never bypass B or W.
        # Completion and invalid-goal triggers are opened at their concrete
        # event source via ``mandatory_event``.  Only a transition to a truly
        # idle robot is discovered here; peer completion inference and
        # consensus retries must not create duplicate mandatory epochs.
        if self.pending_cells and reason == "robot_idle":
            epoch = self._admit_pending(float(now_s), reason, mandatory=True)
            contexts = self._contexts.get(robot.rid, [])
            if contexts:
                context = contexts.pop(0)
                if not contexts:
                    self._contexts.pop(robot.rid, None)
                self._active_epoch_by_robot[robot.rid] = context[0]
                return context
            return epoch.epoch_id, epoch.trigger_reason
        # Intrinsic consensus/retry calls remain part of the most recent
        # logical trigger epoch. Calls before any trigger are intentionally
        # unassociated rather than fabricating polling epochs.
        return self._active_epoch_by_robot.get(robot.rid), str(reason)

    def record_allocator_call(
        self,
        robot_id: str,
        now_s: float,
        duration_ns: int,
        epoch_id: Optional[int],
        trigger_reason: str,
    ) -> AllocatorCallRecord:
        record = AllocatorCallRecord(
            call_id=len(self.allocator_calls) + 1,
            robot_id=str(robot_id),
            mission_time_s=float(now_s),
            duration_ns=max(0, int(duration_ns)),
            epoch_id=epoch_id,
            trigger_reason=str(trigger_reason),
        )
        self.allocator_calls.append(record)
        epoch = self.epoch_by_id(epoch_id)
        if epoch is not None:
            epoch.allocator_call_ids.append(record.call_id)
            epoch.allocator_time_ns += record.duration_ns
            if robot_id not in epoch.called_robot_ids:
                epoch.called_robot_ids.append(robot_id)
            if set(epoch.expected_robot_ids).issubset(epoch.called_robot_ids):
                epoch.closed_time_s = float(now_s)
        return record

    def epoch_by_id(self, epoch_id: Optional[int]) -> Optional[AllocationEpoch]:
        if epoch_id is None:
            return None
        return next((epoch for epoch in self.epochs if epoch.epoch_id == epoch_id), None)

    def has_queued_context(self, robot_id: str) -> bool:
        return bool(self._contexts.get(str(robot_id)))

    def _admit_pending(self, now_s: float, reason: str, mandatory: bool) -> AllocationEpoch:
        if not self.pending_cells:
            raise RuntimeError("cannot open an admission epoch with an empty pending queue")
        pending_depth = self.pending_count
        oldest_age = self.oldest_pending_age_s(now_s)
        self._sample(now_s, f"trigger:{reason}")
        cells = list(self.pending_cells)
        self.pending_cells.clear()
        for cell in cells:
            self.world.admit_task(cell, now_s)
        task_ids = [self.world.target_records[cell].task_id for cell in cells]
        epoch = self._open_epoch(
            now_s,
            reason,
            mandatory=mandatory,
            pending_depth=pending_depth,
            oldest_age_s=oldest_age,
            admitted_task_ids=task_ids,
            expected_robot_ids=sorted(self.robots),
            piggybacked_pending=mandatory,
        )
        for robot in self.robots.values():
            robot.admit_tasks(cells, epoch.epoch_id, reason)
        self._set_contexts(epoch, self.robots)
        self._sample(now_s, f"admit:{reason}")
        return epoch

    def _open_epoch(
        self,
        now_s: float,
        reason: str,
        mandatory: bool,
        pending_depth: int,
        oldest_age_s: float,
        admitted_task_ids: List[Any],
        expected_robot_ids: List[str],
        piggybacked_pending: bool = False,
    ) -> AllocationEpoch:
        epoch = AllocationEpoch(
            epoch_id=self._next_epoch_id,
            opened_time_s=float(now_s),
            trigger_reason=str(reason),
            mandatory=bool(mandatory),
            piggybacked_pending=bool(piggybacked_pending),
            pending_depth_before=int(pending_depth),
            oldest_pending_age_s=float(oldest_age_s),
            admitted_task_ids=list(admitted_task_ids),
            expected_robot_ids=list(expected_robot_ids),
        )
        self._next_epoch_id += 1
        self.epochs.append(epoch)
        return epoch

    def _set_contexts(self, epoch: AllocationEpoch, robots: Mapping[str, "RobotShell"]) -> None:
        for rid in robots:
            self._active_epoch_by_robot[str(rid)] = epoch.epoch_id
            self._contexts.setdefault(str(rid), []).append(
                (epoch.epoch_id, epoch.trigger_reason)
            )

    def _sample(self, now_s: float, event: str) -> None:
        self.queue_samples.append(QueueSample(
            time_s=float(now_s),
            depth=self.pending_count,
            oldest_age_s=self.oldest_pending_age_s(now_s),
            event=event,
        ))


def _percentile(values: Iterable[float], proportion: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    return ordered[max(0, ceil(proportion * len(ordered)) - 1)]


def build_online_metrics(state: "TrialState") -> dict:
    """Return canonical, JSON/CSV-safe metrics for one online condition."""

    scheduler = state.reallocation_scheduler
    calls = scheduler.allocator_calls if scheduler is not None else []
    call_times = [call.duration_s for call in calls]
    records = list(state.world.target_records.values())
    completed = [record for record in records if record.completed]
    release_to_assignment = [
        record.first_assignment_time_s - record.released_time_s
        for record in records
        if record.first_assignment_time_s is not None and record.released_time_s is not None
    ]
    release_to_completion = [
        record.first_completion_time_s - record.released_time_s
        for record in completed
        if record.released_time_s is not None
    ]
    queue = scheduler.queue_samples if scheduler is not None else []
    epochs = scheduler.epochs if scheduler is not None else []
    allocator_time_s = sum(call_times)
    allocator_parallel_s = allocator_parallel_critical_path_s(calls)
    simulated_s = state.simulated_execution_time_s
    mission_serial_s = simulated_s + allocator_time_s
    mission_s = simulated_s + allocator_parallel_s
    if abs(mission_s - state.mission_elapsed_time_s) > 1e-9:
        raise AssertionError("mission time arithmetic invariant failed")
    total_steps = sum(robot.counters.steps_total for robot in state.robots.values())
    assignment_mean = mean(release_to_assignment) if release_to_assignment else 0.0
    completion_mean = mean(release_to_completion) if release_to_completion else 0.0
    return {
        "trial_id": state.scenario.trial_id,
        "all_tasks_completed": state.world.all_targets_completed(),
        "max_robot_steps": max((robot.counters.steps_total for robot in state.robots.values()), default=0),
        "total_team_steps": total_steps,
        # The legacy event clock already combines movement, turn, idle, and
        # communication semantics, so it is retained as the physical/execution
        # component and no unsupported decomposition is invented here.
        "movement_time_s": simulated_s,
        "other_execution_time_s": 0.0,
        "simulated_execution_time_s": simulated_s,
        "release_time_axis": "simulated_execution_time_s",
        "execution_time_accounting": "event_clock_includes_move_turn_idle_and_communication",
        "cumulative_allocator_time_s": allocator_time_s,
        "allocator_time_aggregation": "team_serial_sum",
        "allocator_parallel_critical_path_time_s": allocator_parallel_s,
        "allocator_parallel_time_definition": "sum_over_epoch_timestamp_groups_of_max_per_robot_duration",
        "mission_elapsed_time_serial_compute_s": mission_serial_s,
        "mission_elapsed_time_s": mission_s,
        "mission_elapsed_time_definition": "simulated_execution_time_s_plus_allocator_parallel_critical_path_time_s",
        "host_program_runtime_s": state.host_program_runtime_s,
        "allocator_call_count": len(calls),
        "allocation_epoch_count": len(epochs),
        "mean_allocator_call_time_s": mean(call_times) if call_times else 0.0,
        "median_allocator_call_time_s": median(call_times) if call_times else 0.0,
        "p95_allocator_call_time_s": _percentile(call_times, 0.95),
        "max_allocator_call_time_s": max(call_times, default=0.0),
        "allocator_time_per_completed_task_s": allocator_time_s / len(completed) if completed else 0.0,
        "mean_release_to_first_assignment_latency_s": assignment_mean,
        "median_release_to_first_assignment_latency_s": median(release_to_assignment) if release_to_assignment else 0.0,
        "p95_release_to_first_assignment_latency_s": _percentile(release_to_assignment, 0.95),
        "mean_release_to_completion_latency_s": completion_mean,
        "median_release_to_completion_latency_s": median(release_to_completion) if release_to_completion else 0.0,
        "p95_release_to_completion_latency_s": _percentile(release_to_completion, 0.95),
        "arrival_induced_trigger_count": sum(epoch.trigger_reason in ARRIVAL_REASONS for epoch in epochs),
        "mandatory_trigger_count": sum(epoch.mandatory for epoch in epochs),
        "mandatory_reallocation_trigger_count": sum(
            epoch.mandatory and epoch.trigger_reason != "initial_allocation"
            for epoch in epochs
        ),
        "initial_allocation_epoch_count": sum(
            epoch.trigger_reason == "initial_allocation" for epoch in epochs
        ),
        "mean_pending_queue_depth": mean(sample.depth for sample in queue) if queue else 0.0,
        "max_pending_queue_depth": max((sample.depth for sample in queue), default=0),
        "mean_pending_age_s": mean(sample.oldest_age_s for sample in queue) if queue else 0.0,
        "max_pending_age_s": max((sample.oldest_age_s for sample in queue), default=0.0),
        "pending_queue_sampling": "event_sampled_release_trigger_and_post_admission_not_time_weighted",
        "trigger_reason_counts": {
            reason: sum(epoch.trigger_reason == reason for epoch in epochs)
            for reason in sorted({epoch.trigger_reason for epoch in epochs})
        },
    }
