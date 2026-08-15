from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from math import ceil, isfinite
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, TYPE_CHECKING, Union

from .types import Cell, TrialScenario
from .timing import DEVICE_ALLOCATOR_TIMER_SCOPE

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from known_visit_sim.comms.bus import MessageBus
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
    group_id: Optional[str] = None
    provider_call_id: str = ""
    logical_context_id: Optional[str] = None
    compute_start_time_s: Optional[float] = None
    compute_completion_time_s: Optional[float] = None
    agx_allocator_duration_ns: int = 0
    agx_choose_goal_duration_ns: Optional[int] = None
    agx_algorithm_epoch_reset_duration_ns: Optional[int] = None
    device_allocator_duration_ns: Optional[int] = None
    device_choose_goal_duration_ns: Optional[int] = None
    algorithm_epoch_reset_duration_ns: Optional[int] = None
    serial_roundtrip_ns: int = 0
    host_serialization_setup_ns: int = 0
    host_total_call_ns: int = 0
    psetup_transaction_ns: int = 0
    device_pre_call_setup_ns: Optional[int] = None
    ptime_result_transaction_ns: int = 0
    host_prepare_cpu_ns: int = 0
    timing_decomposition_schema: int = 0
    host_serialization_setup_measured: bool = False
    device_allocator_timer_scope: str = ""
    serial_roundtrip_definition: str = ""
    timing_source: str = "legacy_host_measurement"
    parity_passed: bool = True
    valid_for_mission: bool = True
    active_task_count: Optional[int] = None
    candidate_count: Optional[int] = None
    current_position: Optional[Cell] = None
    authoritative_goal: Optional[Cell] = None
    outbound_message_sha256: str = ""
    pre_state_sha256: str = ""
    authoritative_post_state_sha256: str = ""
    device_goal: Optional[Cell] = None
    device_message_sha256: str = ""
    device_post_state_sha256: str = ""
    call_class: str = "allocator_call"
    allocator_input_event_count: int = 0
    recovery_invoked: bool = False
    board_id: str = ""
    serial_device: str = ""
    attempt_id: str = ""
    physical_measurement_index: int = 0
    hardware_validated: bool = False
    provider_metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.duration_ns = max(0, int(self.duration_ns))
        for name in (
            "serial_roundtrip_ns",
            "host_serialization_setup_ns",
            "host_total_call_ns",
            "psetup_transaction_ns",
            "ptime_result_transaction_ns",
            "host_prepare_cpu_ns",
        ):
            setattr(self, name, max(0, int(getattr(self, name))))
        if self.device_pre_call_setup_ns is not None:
            self.device_pre_call_setup_ns = max(
                0, int(self.device_pre_call_setup_ns)
            )
        self.timing_decomposition_schema = max(
            0, int(self.timing_decomposition_schema)
        )
        self.allocator_input_event_count = max(
            0, int(self.allocator_input_event_count)
        )
        if self.device_allocator_duration_ns is None:
            self.device_allocator_duration_ns = self.duration_ns
        else:
            self.device_allocator_duration_ns = max(
                0, int(self.device_allocator_duration_ns)
            )
            # ``duration_ns`` remains the backward-compatible device-duration
            # column.  It is never USB roundtrip or setup time.
            self.duration_ns = self.device_allocator_duration_ns
        self.agx_allocator_duration_ns = max(
            0, int(self.agx_allocator_duration_ns)
        )
        (
            self.agx_choose_goal_duration_ns,
            self.agx_algorithm_epoch_reset_duration_ns,
        ) = self._validated_allocator_components(
            "AGX",
            self.agx_allocator_duration_ns,
            self.agx_choose_goal_duration_ns,
            self.agx_algorithm_epoch_reset_duration_ns,
        )
        (
            self.device_choose_goal_duration_ns,
            self.algorithm_epoch_reset_duration_ns,
        ) = self._validated_allocator_components(
            "device",
            int(self.device_allocator_duration_ns),
            self.device_choose_goal_duration_ns,
            self.algorithm_epoch_reset_duration_ns,
        )
        if self.compute_start_time_s is None:
            self.compute_start_time_s = float(self.mission_time_s)
        if self.compute_completion_time_s is None:
            self.compute_completion_time_s = (
                float(self.compute_start_time_s) + self.duration_s
            )
        if self.logical_context_id is None:
            self.logical_context_id = str(self.robot_id)

    @staticmethod
    def _validated_allocator_components(
        label: str,
        total_ns: int,
        choose_goal_ns: Optional[int],
        epoch_reset_ns: Optional[int],
    ) -> tuple[int, int]:
        if choose_goal_ns is None and epoch_reset_ns is None:
            choose_goal_ns = int(total_ns)
            epoch_reset_ns = 0
        elif choose_goal_ns is None or epoch_reset_ns is None:
            raise ValueError(f"{label} allocator timing split is incomplete")
        choose_goal_ns = int(choose_goal_ns)
        epoch_reset_ns = int(epoch_reset_ns)
        if choose_goal_ns < 0 or epoch_reset_ns < 0:
            raise ValueError(f"{label} allocator timing split must be non-negative")
        if int(total_ns) != choose_goal_ns + epoch_reset_ns:
            raise ValueError(
                f"{label} allocator duration must equal choose_goal plus "
                "algorithm epoch reset"
            )
        return choose_goal_ns, epoch_reset_ns

    @property
    def duration_s(self) -> float:
        return self.duration_ns / 1_000_000_000.0

    @property
    def device_allocator_duration_s(self) -> float:
        return int(self.device_allocator_duration_ns or 0) / 1_000_000_000.0

    @property
    def agx_allocator_duration_s(self) -> float:
        return int(self.agx_allocator_duration_ns) / 1_000_000_000.0

    @property
    def agx_choose_goal_duration_s(self) -> float:
        return int(self.agx_choose_goal_duration_ns or 0) / 1_000_000_000.0

    @property
    def agx_algorithm_epoch_reset_duration_s(self) -> float:
        return int(
            self.agx_algorithm_epoch_reset_duration_ns or 0
        ) / 1_000_000_000.0

    @property
    def device_choose_goal_duration_s(self) -> float:
        return int(self.device_choose_goal_duration_ns or 0) / 1_000_000_000.0

    @property
    def algorithm_epoch_reset_duration_s(self) -> float:
        return int(self.algorithm_epoch_reset_duration_ns or 0) / 1_000_000_000.0

    @property
    def serial_roundtrip_s(self) -> float:
        return int(self.serial_roundtrip_ns) / 1_000_000_000.0

    @property
    def host_serialization_setup_s(self) -> float:
        return int(self.host_serialization_setup_ns) / 1_000_000_000.0

    @property
    def psetup_transaction_s(self) -> float:
        return int(self.psetup_transaction_ns) / 1_000_000_000.0

    @property
    def device_pre_call_setup_s(self) -> Optional[float]:
        return (
            None
            if self.device_pre_call_setup_ns is None
            else int(self.device_pre_call_setup_ns) / 1_000_000_000.0
        )

    @property
    def ptime_result_transaction_s(self) -> float:
        return int(self.ptime_result_transaction_ns) / 1_000_000_000.0

    @property
    def host_prepare_cpu_s(self) -> float:
        return int(self.host_prepare_cpu_ns) / 1_000_000_000.0

    def to_dict(self) -> dict:
        row = asdict(self)
        row["duration_s"] = self.duration_s
        row["device_allocator_duration_s"] = self.device_allocator_duration_s
        row["agx_allocator_duration_s"] = self.agx_allocator_duration_s
        row["agx_choose_goal_duration_s"] = self.agx_choose_goal_duration_s
        row["agx_algorithm_epoch_reset_duration_s"] = (
            self.agx_algorithm_epoch_reset_duration_s
        )
        row["device_choose_goal_duration_s"] = (
            self.device_choose_goal_duration_s
        )
        row["algorithm_epoch_reset_duration_s"] = (
            self.algorithm_epoch_reset_duration_s
        )
        row["serial_roundtrip_s"] = self.serial_roundtrip_s
        row["host_serialization_setup_s"] = self.host_serialization_setup_s
        row["host_total_call_s"] = self.host_total_call_ns / 1_000_000_000.0
        row["psetup_transaction_s"] = self.psetup_transaction_s
        row["device_pre_call_setup_s"] = self.device_pre_call_setup_s
        row["ptime_result_transaction_s"] = self.ptime_result_transaction_s
        row["host_prepare_cpu_s"] = self.host_prepare_cpu_s
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
    terminal_residual: bool = False
    announcement_delivery_time_s_by_robot: Dict[str, float] = field(default_factory=dict)

    @property
    def admitted_count(self) -> int:
        return len(self.admitted_task_ids)

    def to_dict(self) -> dict:
        row = asdict(self)
        row["admitted_count"] = self.admitted_count
        row["allocator_time_s"] = self.allocator_time_ns / 1_000_000_000.0
        row["initial_call_phase_completed_time_s"] = self.closed_time_s
        row["closure_semantics"] = "first_expected_allocator_call_per_robot_not_consensus_convergence"
        row["announcement_delivery_count"] = len(
            self.announcement_delivery_time_s_by_robot
        )
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
    "terminal_residual",
}
MANDATORY_REASONS = {"task_completion", "invalid_goal", "robot_idle"}


class OnlineReallocationScheduler:
    """Global environment scheduler above otherwise unchanged allocators."""

    def __init__(
        self,
        world: "World",
        policy: ReallocationPolicy,
        bus: Optional["MessageBus"] = None,
    ) -> None:
        self.world = world
        self.policy = ReallocationPolicy.from_spec(policy)
        self.bus = bus
        self.robots: Dict[str, "RobotShell"] = {}
        self.pending_cells: List[Cell] = []
        self.epochs: List[AllocationEpoch] = []
        self.queue_samples: List[QueueSample] = []
        self.allocator_calls: List[AllocatorCallRecord] = []
        self._next_epoch_id = 1
        self._contexts: Dict[str, List[tuple[int, str]]] = {}
        self._delivered_admission_contexts: Set[tuple[str, int]] = set()

    def attach_robots(self, robots: Mapping[str, "RobotShell"]) -> None:
        self.robots = dict(robots)
        if self.bus is None and self.robots:
            self.bus = next(iter(self.robots.values())).bus
        if self.bus is not None and any(
            robot.bus is not self.bus for robot in self.robots.values()
        ):
            raise ValueError("all online robots and the scheduler must share one message bus")
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
            for record in initially_admitted:
                self.world.record_admission_metadata(
                    record.cell, epoch.epoch_id, epoch.trigger_reason, False
                )
            self._broadcast_admission(
                epoch,
                [record.cell for record in initially_admitted],
            )

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
        epochs: List[AllocationEpoch] = []
        if self.policy.mode == "eager":
            while self.pending_cells:
                epochs.append(
                    self._admit_pending(
                        now_s,
                        "task_arrival_eager",
                        mandatory=False,
                        limit=1,
                    )
                )
        else:
            # A threshold epoch contains exactly B tasks.  Simultaneous bursts
            # may therefore create multiple epochs at the same timestamp while
            # preserving a final sub-B residual in the pending queue.
            while self.pending_count >= self.policy.batch_size:
                epochs.append(
                    self._admit_pending(
                        now_s,
                        "batch_threshold",
                        mandatory=False,
                        limit=self.policy.batch_size,
                    )
                )
        terminal = self.maybe_admit_terminal_residual(now_s)
        if terminal is not None:
            epochs.append(terminal)
        return epochs[-1] if epochs else None

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
        """Open a bookkeeping epoch without altering pending-task admission.

        This compatibility API records genuine robot events.  Completion,
        invalid-goal, and idle events never bypass B or W and never expose
        pending tasks.
        """

        if reason not in MANDATORY_REASONS:
            raise ValueError(f"unsupported mandatory trigger reason: {reason}")
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
        return epoch

    def register_admission_delivery(
        self,
        robot_id: str,
        epoch_id: int,
        trigger_reason: Optional[str] = None,
        delivered_at_s: Optional[float] = None,
    ) -> bool:
        """Register allocator context only after a robot receives an announcement.

        The environment may know that an epoch exists when it is opened, but a
        robot cannot act on that epoch until its reliable task-admission message
        crosses the bus delivery boundary. Duplicate delivery registration is
        idempotent.
        """

        rid = str(robot_id)
        if rid not in self.robots:
            raise KeyError(f"unknown robot for admission delivery: {rid}")
        epoch = self.epoch_by_id(int(epoch_id))
        if epoch is None or not epoch.admitted_task_ids:
            raise ValueError("admission delivery must reference an admission epoch")
        if rid not in epoch.expected_robot_ids:
            raise ValueError(f"robot {rid} is not expected for epoch {epoch.epoch_id}")
        if trigger_reason is not None and str(trigger_reason) != epoch.trigger_reason:
            raise ValueError("admission delivery trigger reason does not match epoch")
        key = (rid, epoch.epoch_id)
        if key in self._delivered_admission_contexts:
            return False
        self._delivered_admission_contexts.add(key)
        self._contexts.setdefault(rid, []).append(
            (epoch.epoch_id, epoch.trigger_reason)
        )
        epoch.announcement_delivery_time_s_by_robot[rid] = float(
            epoch.opened_time_s if delivered_at_s is None else delivered_at_s
        )
        return True

    def note_admission_received(
        self,
        robot_id: str,
        epoch_id: int,
        trigger_reason: Optional[str] = None,
        delivered_at_s: Optional[float] = None,
    ) -> bool:
        """Compatibility name used by ``RobotShell`` at message receipt."""

        rid = str(robot_id)
        if delivered_at_s is None:
            robot = self.robots.get(rid)
            delivered_at_s = getattr(robot, "_now", None)
        return self.register_admission_delivery(
            rid,
            epoch_id,
            trigger_reason,
            delivered_at_s=delivered_at_s,
        )

    def before_allocator_call(
        self, robot: "RobotShell", now_s: float, reason: str
    ) -> tuple[Optional[int], str]:
        contexts = self._contexts.get(robot.rid, [])
        if contexts:
            context = contexts.pop(0)
            if not contexts:
                self._contexts.pop(robot.rid, None)
            return context
        # Consensus, completion, invalid-goal, and recovery calls are
        # autonomous agent activity. They retain their causal reason and are
        # never charged to the most recent admission epoch.
        return None, str(reason)

    def record_allocator_call(
        self,
        robot_id: str,
        now_s: float,
        duration_ns: int,
        epoch_id: Optional[int],
        trigger_reason: str,
        **causal_fields: Any,
    ) -> AllocatorCallRecord:
        record = AllocatorCallRecord(
            call_id=len(self.allocator_calls) + 1,
            robot_id=str(robot_id),
            mission_time_s=float(now_s),
            duration_ns=max(0, int(duration_ns)),
            epoch_id=epoch_id,
            trigger_reason=str(trigger_reason),
            **causal_fields,
        )
        self.allocator_calls.append(record)
        epoch = self.epoch_by_id(epoch_id)
        if epoch is not None:
            epoch.allocator_call_ids.append(record.call_id)
            epoch.allocator_time_ns += record.duration_ns
            if robot_id not in epoch.called_robot_ids:
                epoch.called_robot_ids.append(robot_id)
            if set(epoch.expected_robot_ids).issubset(epoch.called_robot_ids):
                completion_times = [
                    item.compute_completion_time_s
                    for item in self.allocator_calls
                    if item.call_id in epoch.allocator_call_ids
                    and item.compute_completion_time_s is not None
                ]
                epoch.closed_time_s = max(completion_times, default=float(now_s))
        return record

    def epoch_by_id(self, epoch_id: Optional[int]) -> Optional[AllocationEpoch]:
        if epoch_id is None:
            return None
        return next((epoch for epoch in self.epochs if epoch.epoch_id == epoch_id), None)

    def has_queued_context(self, robot_id: str) -> bool:
        return bool(self._contexts.get(str(robot_id)))

    def maybe_admit_terminal_residual(
        self, now_s: float
    ) -> Optional[AllocationEpoch]:
        """Admit the sole sub-bound residual after all prior work is complete."""

        if not self.pending_cells or self.unreleased_count != 0:
            return None
        admitted = [
            record
            for record in self.world.target_records.values()
            if record.admission_time_s is not None
        ]
        if any(not record.completed for record in admitted):
            return None
        if (
            self.policy.mode in {"count", "bounded"}
            and self.pending_count >= self.policy.batch_size
        ):
            raise AssertionError(
                "a full threshold batch remained pending until terminal drain"
            )
        return self._admit_pending(
            float(now_s),
            "terminal_residual",
            mandatory=False,
            terminal_residual=True,
        )

    # Readable alias for event-loop call sites.
    terminal_residual_due = maybe_admit_terminal_residual

    def _admit_pending(
        self,
        now_s: float,
        reason: str,
        mandatory: bool,
        *,
        limit: Optional[int] = None,
        terminal_residual: bool = False,
    ) -> AllocationEpoch:
        if not self.pending_cells:
            raise RuntimeError("cannot open an admission epoch with an empty pending queue")
        pending_depth = self.pending_count
        oldest_age = self.oldest_pending_age_s(now_s)
        self._sample(now_s, f"trigger:{reason}")
        admission_count = self.pending_count if limit is None else int(limit)
        if admission_count <= 0 or admission_count > self.pending_count:
            raise ValueError("admission limit must select at least one pending task")
        cells = list(self.pending_cells[:admission_count])
        del self.pending_cells[:admission_count]
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
            piggybacked_pending=False,
            terminal_residual=terminal_residual,
        )
        for cell in cells:
            self.world.record_admission_metadata(
                cell,
                epoch.epoch_id,
                epoch.trigger_reason,
                terminal_residual,
            )
        self._broadcast_admission(epoch, cells)
        self._sample(now_s, f"admit:{reason}")
        return epoch

    def _broadcast_admission(
        self, epoch: AllocationEpoch, cells: Sequence[Cell]
    ) -> None:
        if self.bus is None:
            raise RuntimeError("online task admission requires a message bus")
        task_ids = [self.world.target_records[cell].task_id for cell in cells]
        self.bus.publish_task_admission(
            {
                "type": "task_admission",
                "epoch_index": int(epoch.epoch_id),
                "epoch_id": int(epoch.epoch_id),
                "trigger_reason": str(epoch.trigger_reason),
                "admission_time_s": float(epoch.opened_time_s),
                "admitted_cells": [list(cell) for cell in cells],
                "task_ids": list(task_ids),
                "terminal_residual": bool(epoch.terminal_residual),
            },
            epoch.opened_time_s,
        )

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
        terminal_residual: bool = False,
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
            terminal_residual=bool(terminal_residual),
        )
        self._next_epoch_id += 1
        self.epochs.append(epoch)
        return epoch

    def _set_contexts(self, epoch: AllocationEpoch, robots: Mapping[str, "RobotShell"]) -> None:
        for rid in robots:
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
    all_calls = scheduler.allocator_calls if scheduler is not None else []
    calls = [call for call in all_calls if call.valid_for_mission and call.parity_passed]
    call_times = [call.device_allocator_duration_s for call in calls]
    agx_call_times = [call.agx_allocator_duration_s for call in calls]
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
    release_to_current_goal = [
        record.first_current_goal_time_s - record.released_time_s
        for record in records
        if record.first_current_goal_time_s is not None
        and record.released_time_s is not None
    ]
    admission_to_current_goal = [
        record.first_current_goal_time_s - record.admission_time_s
        for record in records
        if record.first_current_goal_time_s is not None
        and record.admission_time_s is not None
    ]
    announcement_receipt_latencies = [
        receipt_s - record.admission_time_s
        for record in records
        if record.admission_time_s is not None
        for receipt_s in record.knowledge_receipt_time_s_by_robot.values()
    ]
    queue = scheduler.queue_samples if scheduler is not None else []
    epochs = scheduler.epochs if scheduler is not None else []
    rp2040_choose_goal_ns = sum(
        int(call.device_choose_goal_duration_ns or 0) for call in calls
    )
    rp2040_epoch_reset_ns = sum(
        int(call.algorithm_epoch_reset_duration_ns or 0) for call in calls
    )
    agx_choose_goal_ns = sum(
        int(call.agx_choose_goal_duration_ns or 0) for call in calls
    )
    agx_epoch_reset_ns = sum(
        int(call.agx_algorithm_epoch_reset_duration_ns or 0) for call in calls
    )
    device_total_ns = sum(
        int(call.device_allocator_duration_ns or 0) for call in calls
    )
    agx_total_ns = sum(int(call.agx_allocator_duration_ns) for call in calls)
    if device_total_ns != rp2040_choose_goal_ns + rp2040_epoch_reset_ns:
        raise AssertionError("RP2040 allocator work decomposition is inconsistent")
    if agx_total_ns != agx_choose_goal_ns + agx_epoch_reset_ns:
        raise AssertionError("AGX allocator work decomposition is inconsistent")
    rp2040_choose_goal_s = rp2040_choose_goal_ns / 1_000_000_000.0
    rp2040_epoch_reset_s = rp2040_epoch_reset_ns / 1_000_000_000.0
    agx_choose_goal_s = agx_choose_goal_ns / 1_000_000_000.0
    agx_epoch_reset_s = agx_epoch_reset_ns / 1_000_000_000.0
    # Define the published totals from the published components so the JSON
    # summary itself preserves the mechanism accounting identity exactly.
    allocator_time_s = rp2040_choose_goal_s + rp2040_epoch_reset_s
    agx_allocator_time_s = agx_choose_goal_s + agx_epoch_reset_s
    grouped_critical_path_s = allocator_parallel_critical_path_s(calls)
    all_tasks_completed = state.world.all_targets_completed()
    mission_s = state.mission_elapsed_time_s if all_tasks_completed else None
    simulated_s = state.simulated_execution_time_s
    if all_tasks_completed and mission_s is not None and abs(mission_s - simulated_s) > 1e-9:
        raise AssertionError("causal event clock must stop at final task completion")
    movement_work_s = sum(
        movement.duration_s for movement in getattr(state, "movement_records", [])
    )
    total_steps = sum(robot.counters.steps_total for robot in state.robots.values())
    assignment_mean = mean(release_to_assignment) if release_to_assignment else 0.0
    completion_mean = mean(release_to_completion) if release_to_completion else 0.0
    current_goal_mean = (
        mean(release_to_current_goal) if release_to_current_goal else 0.0
    )
    hardware_call_count = sum(call.hardware_validated for call in calls)
    hardware_validated = bool(calls) and hardware_call_count == len(calls)
    recovery_attempt_count = sum(call.recovery_invoked for call in calls)
    recovery_state_change_count = sum(
        int(getattr(robot, "_stall_recovery_count", 0))
        for robot in state.robots.values()
    )
    bus = state.bus.counters
    return {
        "trial_id": state.scenario.trial_id,
        "all_tasks_completed": all_tasks_completed,
        "max_robot_steps": max((robot.counters.steps_total for robot in state.robots.values()), default=0),
        "total_team_steps": total_steps,
        "movement_time_s": movement_work_s,
        "movement_time_aggregation": "sum_of_explicit_robot_traversal_intervals",
        "movement_event_count": len(getattr(state, "movement_records", [])),
        "movement_timing_model": str(getattr(state, "movement_timing_model", "")),
        "movement_timing_seed": int(getattr(state, "movement_timing_seed", 0)),
        "movement_timing_trace_id": str(
            getattr(state, "movement_timing_trace_id", "")
        ),
        "simulated_execution_time_s": simulated_s,
        "release_time_axis": "absolute_causal_mission_time_s",
        "execution_time_accounting": "causal_event_clock_includes_overlapping_per_robot_compute_and_explicit_movement",
        "cumulative_allocator_time_s": allocator_time_s,
        "device_allocator_processor_work_s": allocator_time_s,
        "device_allocator_measurement_source": str(
            getattr(state, "timing_provider_name", "")
        ),
        "device_allocator_hardware_validated": hardware_validated,
        "hardware_validated_allocator_call_count": hardware_call_count,
        "rp2040_allocator_processor_work_s": allocator_time_s,
        "rp2040_choose_goal_processor_work_s": rp2040_choose_goal_s,
        "rp2040_epoch_reset_processor_work_s": rp2040_epoch_reset_s,
        "W_alloc_rp2040_s": allocator_time_s,
        "allocator_time_aggregation": "sum_of_valid_device_allocator_durations_processor_seconds",
        "allocator_processor_timer_scope": DEVICE_ALLOCATOR_TIMER_SCOPE,
        "allocator_processor_timer_excludes_messaging": True,
        "cumulative_agx_allocator_time_s": agx_allocator_time_s,
        "agx_allocator_processor_work_s": agx_allocator_time_s,
        "agx_choose_goal_processor_work_s": agx_choose_goal_s,
        "agx_epoch_reset_processor_work_s": agx_epoch_reset_s,
        "W_alloc_agx_s": agx_allocator_time_s,
        # The former post-hoc additive field is neutralized.  The comparable
        # group diagnostic remains available under an explicitly non-mission
        # name and must never be added to the causal event clock.
        "allocator_parallel_critical_path_time_s": 0.0,
        "allocator_parallel_time_definition": "deprecated_zero_not_added_to_causal_mission",
        "sum_same_start_group_max_device_duration_s": grouped_critical_path_s,
        "mission_elapsed_time_s": mission_s,
        "mission_elapsed_time_definition": "final_required_task_completion_timestamp_minus_mission_start",
        "algorithmic_horizon_time_s": (
            None if all_tasks_completed else simulated_s
        ),
        "algorithmic_horizon_time_definition": (
            "causal_event_clock_at_predeclared_algorithmic_noncompletion_horizon; "
            "not mission elapsed time"
        ),
        "host_program_runtime_s": state.host_program_runtime_s,
        "allocator_call_count": len(calls),
        "allocator_input_event_count": sum(
            call.allocator_input_event_count for call in calls
        ),
        "recovery_attempt_count": recovery_attempt_count,
        "recovery_state_change_count": recovery_state_change_count,
        "periodic_idle_allocator_polling_enabled": False,
        "task_knowledge_source": "authenticated_task_admission_and_peer_messages_only",
        "messages_sent_total": bus.sent_total,
        "messages_delivered_total": bus.delivered_total,
        "messages_dropped_total": bus.dropped_total,
        "core_messages_sent_total": bus.core_sent_total,
        "allocation_messages_sent_total": bus.allocation_sent_total,
        "logical_message_payload_bytes_sent_total": (
            bus.payload_bytes_sent_total
        ),
        "logical_core_payload_bytes_sent_total": (
            bus.core_payload_bytes_sent_total
        ),
        "logical_allocation_payload_bytes_sent_total": (
            bus.allocation_payload_bytes_sent_total
        ),
        "logical_message_payload_bytes_delivered_total": (
            bus.payload_bytes_delivered_total
        ),
        "logical_message_payload_bytes_dropped_total": (
            bus.payload_bytes_dropped_total
        ),
        "logical_payload_bytes_definition": (
            "compact canonical JSON payload before provider-specific wire "
            "compression, framing, fan-out, or transport"
        ),
        "logical_payload_bytes_sent_by_topic": dict(
            sorted(bus.payload_bytes_sent_by_topic.items())
        ),
        "invalid_allocator_call_count": len(all_calls) - len(calls),
        "compute_group_count": int(getattr(state, "compute_group_count", 0)),
        "timing_provider": str(getattr(state, "timing_provider_name", "")),
        "causal_timing_enabled": bool(getattr(state, "causal_timing_enabled", False)),
        "algorithmic_status": (
            "completed"
            if state.world.all_targets_completed()
            else "incomplete"
        ),
        "algorithmic_failure_type": getattr(
            state, "algorithmic_failure_type", None
        ),
        "causal_event_horizon_events": int(
            getattr(state, "causal_event_horizon_events", 0)
        ),
        "causal_stagnation_horizon_events": int(
            getattr(state, "causal_stagnation_horizon_events", 0)
        ),
        "allocation_epoch_count": len(epochs),
        "mean_allocator_call_time_s": mean(call_times) if call_times else 0.0,
        "median_allocator_call_time_s": median(call_times) if call_times else 0.0,
        "p95_allocator_call_time_s": _percentile(call_times, 0.95),
        "max_allocator_call_time_s": max(call_times, default=0.0),
        "mean_rp2040_allocator_call_time_s": mean(call_times) if call_times else 0.0,
        "median_rp2040_allocator_call_time_s": median(call_times) if call_times else 0.0,
        "p95_rp2040_allocator_call_time_s": _percentile(call_times, 0.95),
        "mean_agx_allocator_call_time_s": mean(agx_call_times) if agx_call_times else 0.0,
        "median_agx_allocator_call_time_s": median(agx_call_times) if agx_call_times else 0.0,
        "p95_agx_allocator_call_time_s": _percentile(agx_call_times, 0.95),
        "rp2040_processor_work_per_call_s": allocator_time_s / len(calls) if calls else 0.0,
        "rp2040_processor_work_per_reallocation_event_s": allocator_time_s / len(epochs) if epochs else 0.0,
        "allocator_time_per_completed_task_s": allocator_time_s / len(completed) if completed else 0.0,
        "rp2040_processor_work_per_completed_task_s": allocator_time_s / len(completed) if completed else 0.0,
        "processor_capacity_fraction": (
            allocator_time_s / (len(state.robots) * mission_s)
            if state.robots and mission_s is not None and mission_s > 0.0 else 0.0
        ),
        "processor_capacity_fraction_definition": "rp2040_processor_work_divided_by_robot_count_times_causal_mission_elapsed",
        "mean_release_to_first_assignment_latency_s": assignment_mean,
        "first_assignment_metric_semantics": (
            "first_allocator_plan_incorporation_or_claim; compare execution "
            "commitment with first_current_goal metrics"
        ),
        "mean_release_to_first_plan_incorporation_latency_s": assignment_mean,
        "median_release_to_first_assignment_latency_s": median(release_to_assignment) if release_to_assignment else 0.0,
        "p95_release_to_first_assignment_latency_s": _percentile(release_to_assignment, 0.95),
        "max_release_to_first_assignment_latency_s": max(release_to_assignment, default=0.0),
        "claim_or_plan_assignment_event_count": sum(
            record.assignment_events for record in records
        ),
        "claim_or_plan_reassignment_count": sum(
            max(0, record.assignment_events - 1) for record in records
        ),
        "mean_release_to_first_current_goal_latency_s": current_goal_mean,
        "median_release_to_first_current_goal_latency_s": (
            median(release_to_current_goal) if release_to_current_goal else 0.0
        ),
        "p95_release_to_first_current_goal_latency_s": _percentile(
            release_to_current_goal, 0.95
        ),
        "max_release_to_first_current_goal_latency_s": max(
            release_to_current_goal, default=0.0
        ),
        "mean_admission_to_first_current_goal_latency_s": (
            mean(admission_to_current_goal) if admission_to_current_goal else 0.0
        ),
        "mean_task_announcement_delivery_latency_s": (
            mean(announcement_receipt_latencies)
            if announcement_receipt_latencies else 0.0
        ),
        "max_task_announcement_delivery_latency_s": max(
            announcement_receipt_latencies, default=0.0
        ),
        "mean_release_to_completion_latency_s": completion_mean,
        "median_release_to_completion_latency_s": median(release_to_completion) if release_to_completion else 0.0,
        "p95_release_to_completion_latency_s": _percentile(release_to_completion, 0.95),
        "max_release_to_completion_latency_s": max(release_to_completion, default=0.0),
        "arrival_induced_trigger_count": sum(epoch.trigger_reason in ARRIVAL_REASONS for epoch in epochs),
        "mandatory_trigger_count": sum(epoch.mandatory for epoch in epochs),
        "mandatory_reallocation_trigger_count": sum(
            epoch.mandatory and epoch.trigger_reason != "initial_allocation"
            for epoch in epochs
        ),
        "initial_allocation_epoch_count": sum(
            epoch.trigger_reason == "initial_allocation" for epoch in epochs
        ),
        "piggybacked_admission_epoch_count": sum(epoch.piggybacked_pending for epoch in epochs),
        "timeout_trigger_count": sum(epoch.trigger_reason == "age_timeout" for epoch in epochs),
        "batch_threshold_trigger_count": sum(epoch.trigger_reason == "batch_threshold" for epoch in epochs),
        "terminal_residual_trigger_count": sum(
            epoch.trigger_reason == "terminal_residual" for epoch in epochs
        ),
        "terminal_residual_admitted_task_count": sum(
            epoch.admitted_count for epoch in epochs if epoch.terminal_residual
        ),
        "ordinary_admission_epoch_count": sum(
            bool(epoch.admitted_task_ids) and not epoch.terminal_residual
            for epoch in epochs
        ),
        # Retained as a zero-valued compatibility field for readers of the
        # superseded architecture. Final-release flushing is no longer legal.
        "final_flush_trigger_count": sum(epoch.trigger_reason == "final_release_flush" for epoch in epochs),
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


def build_zero_compute_pair_metrics(
    causal_state: "TrialState", zero_compute_state: "TrialState"
) -> dict[str, Any]:
    """Derive the paired causal allocation effect for one immutable condition."""

    if causal_state.scenario.trial_id != zero_compute_state.scenario.trial_id:
        raise ValueError("counterfactual pair has different trial IDs")
    if causal_state.scenario.targets != zero_compute_state.scenario.targets:
        raise ValueError("counterfactual pair has different scenarios")
    left_scheduler = causal_state.reallocation_scheduler
    right_scheduler = zero_compute_state.reallocation_scheduler
    if left_scheduler is None or right_scheduler is None:
        raise ValueError("counterfactual pairing requires online trials")
    if left_scheduler.policy != right_scheduler.policy:
        raise ValueError("counterfactual pair has different policies")
    left_releases = [
        record.release_time_s for record in causal_state.world.target_records.values()
    ]
    right_releases = [
        record.release_time_s for record in zero_compute_state.world.target_records.values()
    ]
    if left_releases != right_releases:
        raise ValueError("counterfactual pair has different release traces")
    causal_s = causal_state.mission_elapsed_time_s
    zero_s = zero_compute_state.mission_elapsed_time_s
    difference_s = causal_s - zero_s
    fraction = difference_s / causal_s if causal_s > 0.0 else 0.0
    return {
        "causal_mission_elapsed_time_s": causal_s,
        "zero_compute_mission_elapsed_time_s": zero_s,
        "D_alloc_s": difference_s,
        "allocation_attributable_mission_fraction": fraction,
        "negative_allocation_effect_flag": difference_s < 0.0,
        "definition": "causal_makespan_difference_not_frozen_call_sum",
    }
