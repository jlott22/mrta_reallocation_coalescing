from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
from math import isfinite
from time import perf_counter_ns
from typing import Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple

from known_visit_sim.core.types import AllocationDecision, Cell, Observation
from known_visit_sim.comms.message import Message


def timed_candidate_filter(method):
    """Record complete candidate discovery, ranking, and truncation calls."""

    @wraps(method)
    def wrapper(self, robot, *args, **kwargs):
        started_ns = perf_counter_ns()
        try:
            return method(self, robot, *args, **kwargs)
        finally:
            counters = getattr(robot, "counters", None)
            samples = getattr(counters, "candidate_filter_time_ns_samples", None)
            if samples is not None:
                samples.append(max(0, perf_counter_ns() - started_ns))

    return wrapper


class RobotAPI(Protocol):
    rid: str
    pos: Cell
    heading: Tuple[int, int]
    grid_size: int

    @property
    def active_tasks(self) -> Set[Cell]: ...

    @property
    def searched(self) -> Set[Cell]: ...

    @property
    def target_p(self) -> Dict[Cell, float]: ...

    @property
    def peer_positions(self) -> Dict[str, Cell]: ...

    def publish_algorithm_message(self, category: str, payload: Dict[str, Any]) -> None: ...


class AllocatorBase:
    """Base class for task-allocation algorithms.

    The simulator does not implement CBAA, ACBBA, DMCHBA, HIPC, PI, or
    Silent Can-Win here. Add those algorithms by subclassing this class.

    Algorithms should make all task-allocation decisions in `choose_goal` and
    may publish allocation-specific messages with `robot.publish_algorithm_message`.
    """

    name: str = "base"
    PROBABILITY_ALPHA: float = 8.0
    _ADMISSION_ALLOCATION_REASONS = frozenset({
        "initial_allocation",
        "task_admission",
        "task_arrival_eager",
        "batch_threshold",
        "age_timeout",
        "terminal_residual",
    })

    def initialize(self, robot: RobotAPI) -> None:
        pass

    def handle_message(self, robot: RobotAPI, message: Message) -> None:
        """Receive droppable allocation-specific messages.

        Core state and collision-intent messages are handled by the simulator
        before this hook. Unknown categories are passed through here.
        """
        pass

    def on_observation(self, robot: RobotAPI, observation: Observation) -> None:
        """Called after the robot enters a cell and possibly visits a target."""
        pass

    def on_task_set_changed(self, robot: RobotAPI) -> bool:
        """Allow an allocator to reset cached paths after local task completion inference."""
        return True

    def on_allocation_epoch(
        self, robot: RobotAPI, reason: str, admitted_tasks: Sequence[Cell]
    ) -> None:
        """Observe task admission without invalidating valid local state.

        Newly admitted tasks are already present in ``robot.active_tasks`` by
        the time this hook runs.  All retained allocators treat a missing
        consensus-table entry as unclaimed, so admission requires no bundle,
        path, claim, or current-goal reset.  Only the cached probability
        normalizer is invalidated because the active set that defines it grew.
        """

        del reason, admitted_tasks
        if hasattr(robot, "_allocation_probability_source_id"):
            setattr(robot, "_allocation_probability_source_id", None)
        if hasattr(robot, "_allocation_probability_normalizer"):
            setattr(robot, "_allocation_probability_normalizer", None)
        if hasattr(robot, "_allocation_probability_values"):
            setattr(robot, "_allocation_probability_values", None)

    def recover_stalled_allocation(self, robot: RobotAPI) -> bool:
        """Return whether allocator-specific local recovery made progress.

        A generic full reset destroys valid ownership and path information and
        is therefore not a safe recovery action.  Consensus allocators that
        can identify one locally stale blocking claim override this hook and
        expire only that claim before their next ordinary allocation call.
        """

        del robot
        return False

    def on_task_completed(
        self,
        robot: RobotAPI,
        cell: Cell,
        reason: str = "",
        local: bool = False,
    ) -> bool:
        """Repair allocator state after a locally learned task completion.

        Subclasses may preserve a valid suffix after their own head task is
        completed or apply their native peer-completion rule.  The return value
        tells the robot shell whether its currently executing goal was
        invalidated.  The default has no allocator-owned state to repair.
        """

        del robot, cell, reason, local
        return False

    def _is_admission_allocation(self, robot: RobotAPI) -> bool:
        """Return whether the current allocator transaction admits new work.

        ``RobotShell`` exposes the frozen trigger while the timed allocator
        transaction runs.  Direct allocator tests and lightweight API stubs do
        not necessarily provide that attribute, so only those callers fall
        back to the older event marker.
        """

        sentinel = object()
        reason = getattr(robot, "_active_allocation_reason", sentinel)
        if reason is sentinel:
            return str(getattr(robot, "last_event", "")) == "task_admission"
        return str(reason) in self._ADMISSION_ALLOCATION_REASONS

    def choose_goal(self, robot: RobotAPI) -> AllocationDecision:
        """Return the next active target cell."""
        raise NotImplementedError

    def debug_state(self) -> Dict[str, Any]:
        return {}

    def _coverage_mode(self, robot: RobotAPI) -> bool:
        # Known-target visits always use route-distance allocation.
        return True

    def _is_active_task(self, robot: RobotAPI, cell: Cell) -> bool:
        return cell in (getattr(robot, "active_tasks", set()) or set())

    def _assigned_row_band(self, robot: RobotAPI) -> Tuple[int, int]:
        """Return this robot's deterministic, approximately even row partition."""
        grid_size = int(getattr(robot, "grid_size", 0))
        cfg = getattr(robot, "cfg", None)
        robot_ids = [str(rid) for rid in getattr(cfg, "robot_ids", [])]
        rid = str(robot.rid)
        if grid_size <= 0:
            raise ValueError("grid_size must be positive")
        if not robot_ids or rid not in robot_ids:
            return (0, grid_size - 1)

        robot_count = len(robot_ids)
        index = robot_ids.index(rid)
        rows_per_robot, extra_rows = divmod(grid_size, robot_count)
        start = index * rows_per_robot + min(index, extra_rows)
        height = rows_per_robot + (1 if index < extra_rows else 0)
        if height <= 0:
            raise ValueError("row-band assignment requires robot_count <= grid_size")
        return (start, start + height - 1)

    def _planning_horizon(self, robot: RobotAPI, default: int) -> int:
        cfg = getattr(robot, "cfg", None)
        override = getattr(cfg, "commitment_horizon", None)
        if override is None:
            return int(default)
        horizon = int(override)
        if horizon <= 0:
            raise ValueError("commitment_horizon must be positive")
        return horizon

    def _candidate_limit(self, robot: RobotAPI) -> Optional[int]:
        cfg = getattr(robot, "cfg", None)
        value = getattr(cfg, "max_candidate_cells", None)
        if value is None:
            value = getattr(self, "MAX_CANDIDATE_CELLS", None)
        if value is None:
            return None
        if isinstance(value, str) and value.lower() == "all":
            return None
        limit = int(value)
        if limit <= 0:
            raise ValueError("max_candidate_cells must be positive or 'all'")
        return limit

    def _filter_candidate_cells(self, robot: RobotAPI, candidates: Sequence[Cell]) -> List[Cell]:
        ordered = list(candidates)
        limit = self._candidate_limit(robot)
        setattr(robot, "candidate_count_before_filter", len(ordered))
        setattr(robot, "candidate_count_after_filter", len(ordered) if limit is None else min(len(ordered), limit))
        setattr(robot, "max_candidate_cells", limit)
        if limit is None or limit >= len(ordered):
            return ordered

        origin = self._normalize_filter_cell(getattr(robot, "pos", None)) or (0, 0)

        def ranking(cell: Cell) -> Tuple[float, int, Cell]:
            probability = self._filter_probability(robot, cell)
            distance = self._manhattan_distance(cell, origin)
            return (-probability, distance, cell)

        filtered = sorted(ordered, key=ranking)[:limit]
        setattr(robot, "candidate_count_after_filter", len(filtered))
        return filtered

    def _unrestricted_candidate_cells(
        self, robot: RobotAPI, candidates: Sequence[Cell]
    ) -> List[Cell]:
        """Return the complete locally known eligible pool for core allocators.

        CBAA, ACBBA, PI, and HIPC use this helper so an experimental candidate
        cap cannot silently hide an admitted task.  Other allocators retain the
        optional candidate-filter sensitivity through ``_filter_candidate_cells``.
        """

        ordered = list(candidates)
        setattr(robot, "candidate_count_before_filter", len(ordered))
        setattr(robot, "candidate_count_after_filter", len(ordered))
        setattr(robot, "max_candidate_cells", None)
        return ordered

    def _filter_probability(self, robot: RobotAPI, cell: Cell) -> float:
        active_count = len(getattr(robot, "active_tasks", set()) or set())
        target_p = getattr(robot, "_allocation_probability_values", None)
        if (
            not isinstance(target_p, dict)
            or getattr(robot, "_allocation_probability_task_count", None)
            != active_count
        ):
            target_p = getattr(robot, "target_p", {}) or {}
        try:
            value = target_p.get(cell, 0.0)
        except AttributeError:
            try:
                value = target_p[cell[1]][cell[0]]
            except Exception:
                value = 0.0
        try:
            probability = float(value)
        except Exception:
            return 0.0
        if not isfinite(probability):
            return 0.0
        return max(0.0, probability)

    def _refresh_allocation_probability_normalizer(self, robot: RobotAPI) -> float:
        """Cache the max active-task target value used by shared allocation cost.

        In known-target runs every active target has target_p=1.0, so this
        normalizer makes all active targets p_norm=1.0 and the shared cost
        reduces to pure distance. Keeping the helper here preserves the same
        algorithm semantics as the clue/coverage simulator without importing
        clue-specific belief behavior.
        """

        target_p = getattr(robot, "target_p", {}) or {}
        candidates = getattr(robot, "active_tasks", set()) or set()
        max_p = 0.0
        for cell in candidates:
            try:
                value = float(target_p.get(cell, 0.0))
            except AttributeError:
                try:
                    value = float(target_p[cell[1]][cell[0]])
                except Exception:
                    value = 0.0
            except Exception:
                value = 0.0
            if isfinite(value) and value > max_p:
                max_p = value
        if max_p <= 0.0 or not isfinite(max_p):
            max_p = 1.0
        setattr(robot, "_allocation_probability_normalizer", float(max_p))
        setattr(robot, "_allocation_probability_source_id", id(target_p))
        if isinstance(target_p, dict):
            setattr(robot, "_allocation_probability_values", dict(target_p))
        setattr(
            robot,
            "_allocation_probability_task_count",
            len(getattr(robot, "active_tasks", set()) or set()),
        )
        return float(max_p)

    def _normalized_allocation_probability(self, robot: RobotAPI, cell: Cell) -> float:
        normalizer = getattr(robot, "_allocation_probability_normalizer", None)
        active_count = len(getattr(robot, "active_tasks", set()) or set())
        cached_count = getattr(robot, "_allocation_probability_task_count", None)
        # RobotShell.target_p is a local-view property and may return a fresh
        # mapping object on every access. Object identity therefore cannot be
        # used as a cache key without turning every score into an O(T) refresh.
        if normalizer is None or cached_count != active_count:
            normalizer = self._refresh_allocation_probability_normalizer(robot)

        try:
            normalizer = float(normalizer)
        except Exception:
            normalizer = 1.0
        if normalizer <= 0.0 or not isfinite(normalizer):
            normalizer = self._refresh_allocation_probability_normalizer(robot)

        probability = self._filter_probability(robot, cell)
        return float(max(0.0, min(1.0, probability / normalizer)))

    def _probability_penalty(self, robot: RobotAPI, cell: Cell) -> float:
        """Return alpha * (1 - normalized probability) with shared alpha=8."""

        try:
            alpha = float(getattr(self, "PROBABILITY_ALPHA", AllocatorBase.PROBABILITY_ALPHA))
        except Exception:
            alpha = AllocatorBase.PROBABILITY_ALPHA
        if alpha < 0.0 or not isfinite(alpha):
            alpha = AllocatorBase.PROBABILITY_ALPHA
        probability = self._normalized_allocation_probability(robot, cell)
        return float(alpha * (1.0 - probability))

    def _probability_adjusted_cost(self, robot: RobotAPI, distance: float, cell: Cell) -> float:
        """Return distance + 8 * (1 - normalized target probability)."""

        try:
            base_distance = float(distance)
        except Exception:
            base_distance = 0.0
        if base_distance < 0.0 or not isfinite(base_distance):
            base_distance = 0.0
        return float(base_distance + self._probability_penalty(robot, cell))

    def _probability_adjusted_score(self, robot: RobotAPI, distance: float, cell: Cell) -> float:
        """Return the higher-is-better negative of the shared adjusted cost."""

        return -self._probability_adjusted_cost(robot, distance, cell)

    def _normalize_filter_cell(self, cell: Any) -> Optional[Cell]:
        try:
            if cell is None or len(cell) != 2:
                return None
            return (int(cell[0]), int(cell[1]))
        except Exception:
            return None

    @staticmethod
    def _manhattan_distance(a: Cell, b: Cell) -> int:
        return abs(int(a[0]) - int(b[0])) + abs(int(a[1]) - int(b[1]))
