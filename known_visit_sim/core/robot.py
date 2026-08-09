from __future__ import annotations

import copy
from collections import deque
from dataclasses import dataclass
from time import perf_counter_ns
from typing import Any, Deque, Dict, List, Mapping, Optional, Set, Tuple

from known_visit_sim.algorithms.base import AllocatorBase
from known_visit_sim.comms.bus import MessageBus
from known_visit_sim.comms.message import Message, topic_for
from known_visit_sim.config import SimConfig
from known_visit_sim.metrics.counters import RobotCounters
from .reallocation import AllocatorCallRecord
from .planner import AStarPlanner
from .types import Cell, DIRS4, Heading, Observation, in_bounds
from .timing import (
    DecisionSignature,
    canonical_sha256,
    parity_encoded_sha256,
    parity_sha256,
    replay_encode_value,
)
from .world import World


@dataclass
class StepResult:
    reason: str
    moved: bool = False
    target_visited: bool = False
    first_completion: bool = False
    time_cost_s: float = 0.0
    action_target: Optional[Cell] = None


@dataclass
class PendingAction:
    kind: str
    target: Optional[Cell] = None
    heading: Optional[Heading] = None


@dataclass
class PreparedCausalAllocation:
    epoch_id: Optional[int]
    trigger_reason: str
    pre_state: Dict[str, Any]
    pre_state_sha256: str
    frozen_peer_positions: Dict[str, Cell]
    previous_task: Optional[Cell]
    previous_task_invalidated: bool
    active_tasks_at_start: Tuple[Cell, ...]
    device_events: Tuple[Dict[str, Any], ...]
    agx_algorithm_epoch_reset_duration_ns: int


@dataclass
class StagedCausalAllocation:
    prepared: PreparedCausalAllocation
    decision: Any
    outbound_payloads: List[Dict[str, Any]]
    agx_duration_ns: int
    agx_choose_goal_duration_ns: int
    agx_algorithm_epoch_reset_duration_ns: int
    post_state: Dict[str, Any]
    signature: DecisionSignature


class RobotShell:
    """Robot wrapper owned by the simulator.

    The task allocator chooses task cells and handles algorithm messages. This shell
    handles movement, target visits, protected collision safety, and
    metrics.
    """

    def __init__(
        self,
        rid: str,
        pos: Cell,
        heading: Heading,
        cfg: SimConfig,
        world: World,
        bus: MessageBus,
        allocator: AllocatorBase,
    ) -> None:
        self.rid = rid
        self.pos = pos
        self.heading = heading
        self.cfg = cfg
        self.grid_size = cfg.grid_size
        self.world = world
        self.bus = bus
        self.allocator = allocator
        self._searched: Set[Cell] = {pos}
        self._active_tasks: Set[Cell] = set(world.admitted_targets)
        self.counters = RobotCounters(rid=rid)
        self.current_goal: Optional[Cell] = None
        self.last_goal: Optional[Cell] = None
        self.last_path: List[Cell] = []
        self.last_next_cell: Optional[Cell] = None
        self.last_event: str = "init"
        self.last_decision_debug: Dict[str, Any] = {}
        self.collision_avoidance_active = False
        self._collision_event_counted_since_move = False
        self._blocked_goal_failures: Dict[Cell, int] = {}
        self._temporary_invalid_task_until: Dict[Cell, float] = {}
        self._blocked_goal_quarantine_level: Dict[Cell, int] = {}
        self._no_goal_since: Optional[float] = None
        self._stall_recovery_count = 0
        self._communicated_collision_intent: Optional[Cell] = None
        self._last_published_state_pos: Optional[Cell] = None
        self._now: float = 0.0
        self.pending_actions: Deque[PendingAction] = deque()
        self._reallocation_scheduler = None
        self._next_allocation_reason: Optional[str] = "initial_allocation"
        self._reported_assignments: Set[Cell] = set()
        self._causal_busy_kind: Optional[str] = None
        self._causal_compute_started_s: Optional[float] = None
        self._causal_inflight_move: Optional[PendingAction] = None
        # External inputs that arrive while compute-blocked share one FIFO;
        # separate admission/message queues would silently reorder them at
        # commit and could change both the next AGX call and board replay.
        self._causal_buffered_inputs: Deque[Tuple[str, Any]] = deque()
        self._causal_staging_publications: Optional[List[Dict[str, Any]]] = None
        self._causal_result_ready = False
        # Mirrors the native resident runtime's idempotent admission-epoch
        # seal.  The initial release-zero set is part of reset state and does
        # not fabricate an epoch callback; delayed admissions advance these.
        self.last_allocation_epoch_index = -1
        self.last_allocation_epoch_reason = ""
        self.last_allocation_epoch_admitted: List[Cell] = []
        # Exact radio-decoded allocator payloads applied to the authoritative
        # desktop context since its previous device call.  A resident device
        # context cannot reconstruct these from the restored physical state;
        # they are drained atomically into the next frozen call instead.
        self._causal_device_events: List[Dict[str, Any]] = []
        # Wall-clock AGX processor work performed by policy-induced epoch
        # callbacks is accumulated until the matching frozen allocator call.
        # Device PSETUP applies and times the same event stream before its
        # choose_goal region, so the two authoritative scopes stay aligned.
        self._causal_pending_agx_epoch_reset_ns = 0

        # Droppable coordination knowledge.
        self._peer_positions: Dict[str, Cell] = {}
        # Protected collision-avoidance cache. Not used for task allocation.
        self._collision_peer_positions: Dict[str, Cell] = {}
        self._collision_peer_intents: Dict[str, Optional[Cell]] = {}
        self.temp_blocked_next: Set[Cell] = set()
        self._active_peer_positions: Optional[Dict[str, Cell]] = None
        self._perception_pending_positions: Dict[str, Cell] = {}
        self._perception_pending_collision_positions: Dict[str, Cell] = {}
        self._perception_pending_collision_intents: Dict[str, Optional[Cell]] = {}
        self._perception_pending_valid = False
        self._perception_initialized = False

        # Local truth/knowledge.
        if not self.world.record_visit(rid, pos):
            self.counters.unique_cells_contributed += 1

        self.bus.register(self)
        self.allocator.initialize(self)

    @property
    def active_tasks(self) -> Set[Cell]:
        return set(self._active_tasks)

    def attach_reallocation_scheduler(self, scheduler: Any) -> None:
        self._reallocation_scheduler = scheduler

    def admit_tasks(
        self, cells: List[Cell], epoch_id: int, trigger_reason: str
    ) -> None:
        if self._causal_busy_kind == "compute":
            self._causal_buffered_inputs.append(
                (
                    "admission",
                    (list(cells), int(epoch_id), str(trigger_reason)),
                )
            )
            return
        self._apply_admitted_tasks(cells, epoch_id, trigger_reason)

    def _apply_admitted_tasks(
        self, cells: List[Cell], epoch_id: int, trigger_reason: str
    ) -> None:
        admitted = [
            cell for cell in cells
            if cell in self.world.admitted_targets and cell not in self._active_tasks
        ]
        if not admitted:
            return
        # A cell traversed before its task existed is not evidence that the
        # newly admitted task has been serviced.  Keep the world's physical
        # visit/revisit history intact, but reopen the cell in every robot's
        # local planning view so all retained allocators can select it.
        for cell in admitted:
            self._searched.discard(cell)
            self.temp_blocked_next.discard(cell)
            self._blocked_goal_failures.pop(cell, None)
            self._blocked_goal_quarantine_level.pop(cell, None)
            self._temporary_invalid_task_until.pop(cell, None)
            if cell == self.pos:
                # Permit a post-admission state publication at an unchanged
                # position; peers must not rely on the pre-admission message.
                self._last_published_state_pos = None
        self._active_tasks.update(admitted)
        self.last_event = "task_admission"
        self.last_allocation_epoch_index = int(epoch_id)
        self.last_allocation_epoch_reason = str(trigger_reason)
        self.last_allocation_epoch_admitted = list(admitted)
        self._causal_device_events.append({
            "kind": "allocation_epoch",
            "payload": {
                "epoch_index": int(epoch_id),
                "trigger_reason": str(trigger_reason),
                "admitted_cells": list(admitted),
            },
        })
        handler = getattr(self.allocator, "on_allocation_epoch", None)
        if callable(handler):
            epoch_started_ns = perf_counter_ns()
            try:
                handler(self, trigger_reason, admitted)
            finally:
                self._causal_pending_agx_epoch_reset_ns += max(
                    0, perf_counter_ns() - epoch_started_ns
                )
        self.current_goal = None
        self._clear_pending_actions()
        self._next_allocation_reason = trigger_reason

    def service_queued_allocation_epochs(self, now_s: float) -> int:
        """Run queued global epoch calls without advancing physical motion.

        Environment arrival interrupts are global.  Servicing their allocator
        phase before any robot moves guarantees that every expected robot call
        is accounted for, even when a nearby robot can complete a task on its
        next physical wake.
        """

        scheduler = self._reallocation_scheduler
        if scheduler is None or not scheduler.has_queued_context(self.rid):
            return 0
        self._now = float(now_s)
        calls = 0
        plan_peer_positions, _, _ = self._promote_perception()
        self._active_peer_positions = plan_peer_positions
        try:
            while scheduler.has_queued_context(self.rid):
                self._next_allocation_reason = None
                self.bus.pump(self._now)
                decision = self._choose_goal_with_metrics("consensus/internal")
                if decision.goal is not None and decision.goal not in self._active_tasks:
                    raise RuntimeError(
                        f"{self.allocator.name} selected inactive/non-target goal {decision.goal}"
                    )
                self.current_goal = decision.goal
                self.last_decision_debug = decision.debug
                self._record_owned_assignments()
                if self.current_goal is not None:
                    self.last_goal = self.current_goal
                    self._no_goal_since = None
                self._publish_allocator_messages()
                calls += 1
        finally:
            self._active_peer_positions = None
            self.collision_avoidance_active = False
        return calls

    @property
    def searched(self) -> Set[Cell]:
        return self._searched

    @property
    def local_searched(self) -> Set[Cell]:
        return self._searched

    @property
    def target_p(self) -> Dict[Cell, float]:
        return {cell: 1.0 for cell in self._active_tasks}

    @property
    def known_obstacles(self) -> Set[Cell]:
        return set()

    @property
    def obstacles(self) -> Set[Cell]:
        return self.known_obstacles

    @property
    def blocked(self) -> Set[Cell]:
        return self.known_obstacles | set(self._temporary_invalid_task_until.keys())

    @property
    def blocked_cells(self) -> Set[Cell]:
        return self.blocked

    @property
    def peer_positions(self) -> Dict[str, Cell]:
        return self._active_peer_positions if self._active_peer_positions is not None else self._peer_positions

    def _publish(self, category: str, payload: Dict[str, Any]) -> None:
        if self._causal_staging_publications is not None:
            staged = dict(payload)
            staged.setdefault("type", category)
            self._causal_staging_publications.append(staged)
            return
        self.bus.publish(self.rid, topic_for(self.rid, category), payload, self._now)

    def publish_algorithm_message(self, category: str, payload: Dict[str, Any]) -> None:
        self._publish(category, payload)

    def publish_state(self) -> None:
        if self._last_published_state_pos == self.pos:
            return
        self._last_published_state_pos = self.pos
        self._publish("state", {"loc": list(self.pos)})

    def publish_collision_intent(self, intent: Optional[Cell]) -> None:
        # Protected: collision avoidance is not part of degraded comm evaluation.
        payload = {"loc": list(self.pos), "intent": list(intent) if intent is not None else None}
        self._publish("collision_intent", payload)

    def _set_collision_intent(self, intent: Optional[Cell]) -> None:
        if intent is None:
            if self._communicated_collision_intent is None:
                return
            self._communicated_collision_intent = None
            self.publish_collision_intent(None)
            return
        normalized = (int(intent[0]), int(intent[1]))
        if self._communicated_collision_intent == normalized:
            return
        self._communicated_collision_intent = normalized
        self.publish_collision_intent(normalized)

    def receive_message(self, message: Message) -> None:
        if self._causal_busy_kind == "compute":
            self._causal_buffered_inputs.append(("message", message))
            return
        category = message.category
        payload = message.payload
        sender = message.sender
        if sender == self.rid:
            return
        if category == "state":
            loc = _payload_cell(payload.get("loc"))
            if loc is not None and in_bounds(loc, self.grid_size):
                self._peer_positions[sender] = loc
                target = self.world.target_records.get(loc)
                if target is not None and target.completed:
                    # The shared world is authoritative even if this delayed
                    # message merely prompted the local cache refresh.  Keep
                    # the retained peer-completion event name so allocator
                    # invalidation behavior is unchanged in static trials.
                    self._complete_task_locally(
                        loc, reason="peer_state_at_target"
                    )
                elif (
                    target is not None
                    and target.admission_time_s is not None
                    and message.created_at_s + 1e-12 >= target.admission_time_s
                ):
                    # A state report created while the task was unreleased or
                    # pending cannot prove post-admission service, even if it
                    # arrives after admission because of link delay.
                    self._complete_task_locally(
                        loc, reason="peer_state_at_target"
                    )
            return
        if category == "collision_intent":
            loc = _payload_cell(payload.get("loc"))
            intent = _payload_cell(payload.get("intent")) if payload.get("intent") is not None else None
            if intent is not None and in_bounds(intent, self.grid_size):
                self._collision_peer_positions[sender] = intent
                self._collision_peer_intents[sender] = intent
            elif loc is not None and in_bounds(loc, self.grid_size):
                self._collision_peer_positions[sender] = loc
                self._collision_peer_intents.pop(sender, None)
            return
        if category in {
            "cbaa_entry",
            "acbba_entry",
            "pi_entry",
            "pi_clear_path",
            "hipc_entry",
            "hipc_clear_bundle",
            "dga_entry",
            "dmchba_entry",
        }:
            self._deliver_allocator_payload(payload)
            return
        self.allocator.handle_message(self, message)

    def step(self, now_s: float, planner: AStarPlanner) -> StepResult:
        self._now = now_s
        self._expire_temporary_invalid_tasks()
        self.bus.pump(now_s)
        if self.pending_actions:
            return self._execute_pending_action(planner)

        return self._plan_next_action(planner)

    @property
    def causal_busy_kind(self) -> Optional[str]:
        return self._causal_busy_kind

    @property
    def causal_is_busy(self) -> bool:
        return self._causal_busy_kind is not None

    def causal_allocator_snapshot(self) -> Dict[str, Any]:
        """Return the five-section logical state used at a call boundary.

        The structure matches replay fixtures.  Hardware adapters may encode
        the contained Python values with their transport codec; keeping the
        unencoded logical values here also makes deterministic tests readable.
        """

        prefixes = (
            "cbaa_", "acbba_", "pi_", "hipc_", "dmchba_", "dga_",
            "candidate_count_", "max_candidate_cells", "_allocation_probability_",
        )
        core_names = {
            "rid", "pos", "heading", "grid_size", "current_goal", "last_goal",
            "last_event", "collision_avoidance_active", "collision_state",
            "_active_peer_positions", "last_allocation_epoch_index",
            "last_allocation_epoch_reason", "last_allocation_epoch_admitted",
        }

        def supported_copy(value: Any) -> Any:
            return replay_encode_value(value)

        robot_attrs: Dict[str, Any] = {}
        for name, value in vars(self).items():
            if name in {
                "_allocation_probability_source_id",
                "_allocation_probability_belief_id",
            }:
                continue
            if name in core_names or name.startswith(prefixes):
                try:
                    robot_attrs[name] = supported_copy(value)
                except Exception:
                    continue
        for name in core_names:
            if name not in robot_attrs:
                try:
                    robot_attrs[name] = supported_copy(getattr(self, name, None))
                except Exception:
                    continue
        views = {
            "searched": supported_copy(set(self.searched)),
            "local_searched": supported_copy(set(self.local_searched)),
            "target_p": supported_copy(dict(self.target_p)),
            "peer_positions": supported_copy(dict(self.peer_positions)),
            "active_tasks": supported_copy(set(self.active_tasks)),
            "known_obstacles": supported_copy(set(self.known_obstacles)),
            "obstacles": supported_copy(set(self.obstacles)),
            "blocked": supported_copy(set(self.blocked)),
            "blocked_cells": supported_copy(set(self.blocked_cells)),
        }
        cfg = {
            name: supported_copy(value)
            for name, value in vars(self.cfg).items()
            if not callable(value)
        }
        cfg["all_tasks"] = supported_copy(list(self.world.target_records))
        allocator_attrs = {
            name: supported_copy(value)
            for name, value in vars(self.allocator).items()
            if not callable(value)
        }
        return {
            "robot_attrs": robot_attrs,
            "views": views,
            "cfg": cfg,
            "belief": {},
            "allocator_attrs": allocator_attrs,
        }

    def causal_allocation_reason(self) -> Optional[str]:
        """Return the reason for a call at this safe boundary, if any."""

        if self.causal_is_busy:
            return None
        scheduler = self._reallocation_scheduler
        if scheduler is not None and scheduler.has_queued_context(self.rid):
            return "consensus/internal"
        # A just-completed call gets one control action before the shell may
        # decide that another intrinsic/idle call is necessary.  Without this
        # latch a zero-goal result would immediately recurse at the same time.
        if self._causal_result_ready:
            return None
        previous_task = self.current_goal
        previous_task_completed = (
            previous_task is not None and previous_task not in self._active_tasks
        )
        if self.current_goal is not None and self.current_goal in self._active_tasks:
            return None
        reason = self._next_allocation_reason
        if reason is not None:
            return reason
        if previous_task_completed:
            return "task_completion"
        if previous_task is not None:
            return "invalid_goal"
        if not self._active_tasks:
            return "robot_idle"
        if self.last_goal is None:
            return "initial_allocation"
        return "consensus/internal"

    def prepare_causal_allocation(
        self, now_s: float, reason: str
    ) -> PreparedCausalAllocation:
        """Freeze one call input without making a result externally visible."""

        if self.causal_is_busy:
            raise RuntimeError(f"robot {self.rid} is already {self._causal_busy_kind}")
        self._now = float(now_s)
        epoch_id: Optional[int] = None
        epoch_reason = str(reason)
        if self._reallocation_scheduler is not None:
            epoch_id, epoch_reason = self._reallocation_scheduler.before_allocator_call(
                self, self._now, reason
            )
        # ``before_allocator_call`` may piggyback pending admissions.  Freeze
        # after that atomic scheduler action so the admitted tasks are visible
        # to the mandatory call itself.
        plan_peer_positions, _, _ = self._promote_perception()
        previous_task = self.current_goal
        previous_task_invalidated = (
            previous_task is not None and previous_task in self._active_tasks
        ) or (
            previous_task is None
            and self.last_goal is not None
            and self.last_goal in self._active_tasks
        )
        self._active_peer_positions = dict(plan_peer_positions)
        pre_state = self.causal_allocator_snapshot()
        self._active_peer_positions = None
        device_events = tuple(
            copy.deepcopy(event)
            for event in self._causal_device_events
        )
        self._causal_device_events.clear()
        agx_epoch_reset_ns = self._causal_pending_agx_epoch_reset_ns
        self._causal_pending_agx_epoch_reset_ns = 0
        self.world.record_allocator_start(self.rid, self._active_tasks, self._now)
        self._next_allocation_reason = None
        self._causal_busy_kind = "compute"
        self._causal_compute_started_s = self._now
        return PreparedCausalAllocation(
            epoch_id=epoch_id,
            trigger_reason=epoch_reason,
            pre_state=pre_state,
            pre_state_sha256=canonical_sha256(pre_state),
            frozen_peer_positions=dict(plan_peer_positions),
            previous_task=previous_task,
            previous_task_invalidated=previous_task_invalidated,
            active_tasks_at_start=tuple(sorted(self._active_tasks)),
            device_events=device_events,
            agx_algorithm_epoch_reset_duration_ns=agx_epoch_reset_ns,
        )

    def stage_causal_allocation(
        self, prepared: PreparedCausalAllocation
    ) -> StagedCausalAllocation:
        """Execute the AGX-authoritative call and retain all visible output."""

        if self._causal_busy_kind != "compute":
            raise RuntimeError("causal allocation was not prepared")
        self._active_peer_positions = dict(prepared.frozen_peer_positions)
        direct_publications: List[Dict[str, Any]] = []
        self._causal_staging_publications = direct_publications
        filter_sample_index = len(self.counters.candidate_filter_time_ns_samples)
        started_ns = perf_counter_ns()
        try:
            try:
                decision = self.allocator.choose_goal(self)
            finally:
                # AGX allocator duration excludes message construction,
                # hashing, snapshotting, serialization, and device setup.
                elapsed_ns = max(0, perf_counter_ns() - started_ns)
            generated = self._allocator_outbound_payloads()
            post_state = self.causal_allocator_snapshot()
        finally:
            self._causal_staging_publications = None
            self._active_peer_positions = None
        nested_filter_ns = sum(
            self.counters.candidate_filter_time_ns_samples[filter_sample_index:]
        )
        filter_calls = (
            len(self.counters.candidate_filter_time_ns_samples) - filter_sample_index
        )
        self.counters.allocator_solve_time_ns_samples.append(
            max(0, elapsed_ns - nested_filter_ns)
        )
        outbound: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for payload in [*direct_publications, *generated]:
            if not isinstance(payload, dict):
                continue
            key = canonical_sha256(payload)
            if key not in seen:
                seen.add(key)
                outbound.append(dict(payload))
        candidate_count = int(
            getattr(
                self,
                "candidate_count_after_filter",
                len(prepared.active_tasks_at_start),
            )
        )
        call_class = self._classify_causal_call(
            prepared.pre_state,
            post_state,
            filter_calls,
            allocation_epoch=any(
                event.get("kind") == "allocation_epoch"
                for event in prepared.device_events
            ),
        )
        signature = DecisionSignature(
            goal=decision.goal,
            active_candidate_count=max(0, candidate_count),
            message_sha256=parity_sha256(outbound),
            post_state_sha256=parity_encoded_sha256(post_state),
            call_class=call_class,
        )
        return StagedCausalAllocation(
            prepared=prepared,
            decision=decision,
            outbound_payloads=outbound,
            agx_duration_ns=(
                elapsed_ns
                + prepared.agx_algorithm_epoch_reset_duration_ns
            ),
            agx_choose_goal_duration_ns=elapsed_ns,
            agx_algorithm_epoch_reset_duration_ns=(
                prepared.agx_algorithm_epoch_reset_duration_ns
            ),
            post_state=post_state,
            signature=signature,
        )

    def _classify_causal_call(
        self,
        pre_state: Mapping[str, Any],
        post_state: Mapping[str, Any],
        filter_calls: int,
        *,
        allocation_epoch: bool = False,
    ) -> str:
        """Mirror the native persistent runtime's mechanism classification."""

        before = pre_state.get("robot_attrs", {})
        after = post_state.get("robot_attrs", {})
        algorithm = str(getattr(self.allocator, "name", "")).upper()
        # A queued allocation epoch invokes the epoch reset hook before the
        # call and is classified as a full solve by the native runtime.
        # ``last_event`` is part of the frozen snapshot after that hook.
        if allocation_epoch or str(self.last_event) in {
            "task_admission", "allocation_epoch"
        }:
            return "full_allocation_solve"
        if algorithm in {"DGA", "DMCHBA"}:
            names = (
                ("dga_generation", "dga_last_reallocation_trigger")
                if algorithm == "DGA" else
                ("dmchba_last_assignment_signature", "dmchba_last_reassignment_reason")
            )
            if any(before.get(name) != after.get(name) for name in names):
                return "full_allocation_solve"
        if algorithm == "HIPC" and filter_calls:
            return "full_allocation_solve"
        if algorithm in {"ACBBA", "PI"}:
            path_name = "acbba_path" if algorithm == "ACBBA" else "pi_path"
            collision_refill = bool(
                algorithm == "ACBBA"
                and after.get("acbba_last_reallocation_trigger")
                == replay_encode_value("collision_avoidance")
            )
            if collision_refill or before.get(path_name) != after.get(path_name):
                return "partial_bundle_refill"
        if filter_calls:
            return "candidate_filter_only"
        return "cached_or_maintenance"

    def complete_causal_allocation(
        self, now_s: float, staged: StagedCausalAllocation
    ) -> StepResult:
        """Commit an authoritative result at its own virtual completion."""

        if self._causal_busy_kind != "compute":
            raise RuntimeError("robot has no in-flight causal allocation")
        if (
            self._causal_compute_started_s is None
            or float(now_s) + 1e-12 < self._causal_compute_started_s
        ):
            raise RuntimeError("allocator completion precedes compute start")
        self._now = float(now_s)
        decision = staged.decision
        if decision.goal is not None and decision.goal not in self._active_tasks:
            # It is legal for a task to have completed elsewhere while this
            # call was in flight.  The buffered peer event will invalidate the
            # stale result immediately after it becomes visible.
            record = self.world.target_records.get(decision.goal)
            if record is None or not record.completed:
                raise RuntimeError(
                    f"{self.allocator.name} selected inactive/non-target goal {decision.goal}"
                )
        self.current_goal = decision.goal
        self.last_decision_debug = dict(getattr(decision, "debug", {}) or {})
        self._record_owned_assignments()
        if self.current_goal is not None:
            if (
                self.current_goal != self.last_goal
                and staged.prepared.previous_task_invalidated
            ):
                self.counters.task_cell_replans += 1
            self.last_goal = self.current_goal
            self._no_goal_since = None
        elif self._active_tasks:
            if self._no_goal_since is None:
                self._no_goal_since = self._now
        else:
            self._no_goal_since = None
        for payload in staged.outbound_payloads:
            category = payload.get("type")
            if isinstance(category, str) and category:
                self._publish(category, payload)
        self._causal_busy_kind = None
        self._causal_compute_started_s = None
        self._drain_causal_compute_buffers()
        self._causal_result_ready = True
        self.last_event = "compute_completed"
        return StepResult(reason="compute_completed", time_cost_s=0.0)

    def _drain_causal_compute_buffers(self) -> None:
        while self._causal_buffered_inputs:
            kind, value = self._causal_buffered_inputs.popleft()
            if kind == "admission":
                cells, epoch_id, reason = value
                self._apply_admitted_tasks(cells, epoch_id, reason)
            elif kind == "message":
                self.receive_message(value)
            else:  # pragma: no cover - private queue construction invariant
                raise AssertionError(f"unknown buffered causal input {kind!r}")

    def causal_control_step(
        self, now_s: float, planner: AStarPlanner
    ) -> StepResult:
        """Advance one robot at a safe control boundary without allocating."""

        if self.causal_is_busy:
            raise RuntimeError(f"robot {self.rid} is busy")
        self._now = float(now_s)
        self._expire_temporary_invalid_tasks()
        if self.causal_allocation_reason() is not None:
            return StepResult(reason="allocation_required", time_cost_s=0.0)
        self._causal_result_ready = False
        current_position_service = self._service_task_at_current_position()
        if current_position_service is not None:
            return current_position_service
        if self.pending_actions:
            return self._start_causal_pending_action(planner)
        return self._causal_plan_route(planner)

    def _causal_plan_route(self, planner: AStarPlanner) -> StepResult:
        if self.current_goal is None:
            self._set_collision_intent(None)
            self.last_event = "no_goal"
            return StepResult(reason="no_goal", time_cost_s=self.cfg.no_goal_delay_s)

        plan_peer_positions, plan_collision_positions, plan_collision_intents = (
            self._promote_perception()
        )
        prior_temp_blocked_next = set(self.temp_blocked_next)
        blocked = set(plan_peer_positions.values())
        blocked.update(prior_temp_blocked_next)
        self.temp_blocked_next.clear()
        blocked.discard(self.pos)
        max_collision_replans = max(1, self.grid_size * self.grid_size)
        for _ in range(max_collision_replans):
            path = planner.plan(
                start=self.pos,
                heading=self.heading,
                goal=self.current_goal,
                target_p=self.target_p,
                searched=self._searched,
                blocked=blocked,
            )
            self.last_path = path
            if len(path) < 2:
                self.counters.path_replans += 1
                if self.current_goal is not None and self.current_goal in prior_temp_blocked_next:
                    backoff = self._maybe_temporarily_invalidate_blocked_goal(self.current_goal)
                    if backoff is not None:
                        return backoff
                self.current_goal = None
                self._notify_invalid_goal_epoch()
                self._set_collision_intent(None)
                self.last_event = "path_failed"
                return StepResult(reason="path_failed", time_cost_s=self.cfg.replan_delay_s)
            next_cell = path[1]
            if not self._collision_blocked_by(
                next_cell, plan_collision_positions, plan_collision_intents
            ):
                self.temp_blocked_next.clear()
                self.last_next_cell = next_cell
                self._queue_actions_for_next_cell(next_cell)
                self._set_collision_intent(next_cell)
                return self._start_causal_pending_action(planner)
            self._record_collision_prevention(next_cell)
            backoff = self._maybe_temporarily_invalidate_blocked_goal(next_cell)
            if backoff is not None:
                return backoff
            blocked.add(next_cell)
        self.current_goal = None
        self._notify_invalid_goal_epoch()
        self._set_collision_intent(None)
        self.last_event = "path_failed"
        return StepResult(reason="path_failed", time_cost_s=self.cfg.replan_delay_s)

    def _start_causal_pending_action(
        self, planner: Optional[AStarPlanner] = None
    ) -> StepResult:
        if not self.pending_actions:
            return StepResult(reason="idle", time_cost_s=self.cfg.no_goal_delay_s)
        action = self.pending_actions.popleft()
        self.last_next_cell = action.target
        if action.kind == "turn":
            if action.heading is not None:
                self.heading = action.heading
            self.last_event = "turn"
            return StepResult(
                reason="turn", time_cost_s=self.cfg.turn_quarter_s,
                action_target=action.target,
            )
        if action.kind == "intent_sync":
            self.last_event = "intent_sync"
            return StepResult(
                reason="intent_sync", time_cost_s=self.cfg.collision_intent_settle_s,
                action_target=action.target,
            )
        if action.kind != "move" or action.target is None:
            self._clear_pending_actions()
            self._notify_invalid_goal_epoch()
            return StepResult(reason="path_failed", time_cost_s=self.cfg.replan_delay_s)
        if self._collision_blocked(action.target):
            self._clear_pending_actions()
            self._record_collision_prevention(action.target)
            backoff = self._maybe_temporarily_invalidate_blocked_goal(action.target)
            if backoff is not None:
                return backoff
            if planner is not None:
                return self._causal_plan_route(planner)
            return StepResult(reason="path_failed", time_cost_s=self.cfg.replan_delay_s)
        self._causal_busy_kind = "move"
        self._causal_inflight_move = action
        self.last_event = "move_started"
        return StepResult(
            reason="move_started", moved=False,
            time_cost_s=self.cfg.async_step_mean_s, action_target=action.target,
        )

    def complete_causal_move(self, now_s: float) -> StepResult:
        """Commit position and task service only after traversal duration."""

        if self._causal_busy_kind != "move" or self._causal_inflight_move is None:
            raise RuntimeError("robot has no in-flight movement")
        action = self._causal_inflight_move
        self._causal_inflight_move = None
        self._causal_busy_kind = None
        self._now = float(now_s)
        return self._commit_move(action)

    def _choose_goal_with_metrics(self, reason: str = "other"):
        """Time one allocator call and separate nested candidate-filter work."""
        epoch_id: Optional[int] = None
        epoch_reason = reason
        if self._reallocation_scheduler is not None:
            epoch_id, epoch_reason = self._reallocation_scheduler.before_allocator_call(
                self, self._now, reason
            )
        started_ns = perf_counter_ns()
        filter_sample_index = len(self.counters.candidate_filter_time_ns_samples)
        try:
            return self.allocator.choose_goal(self)
        finally:
            elapsed_ns = max(0, perf_counter_ns() - started_ns)
            nested_filter_ns = sum(
                self.counters.candidate_filter_time_ns_samples[filter_sample_index:]
            )
            self.counters.allocator_time_ns_samples.append(elapsed_ns)
            self.counters.allocator_solve_time_ns_samples.append(
                max(0, elapsed_ns - nested_filter_ns)
            )
            if self._reallocation_scheduler is not None:
                call_record = self._reallocation_scheduler.record_allocator_call(
                    self.rid, self._now, elapsed_ns, epoch_id, epoch_reason
                )
            else:
                call_record = AllocatorCallRecord(
                    call_id=len(self.counters.allocator_call_records) + 1,
                    robot_id=self.rid,
                    mission_time_s=self._now,
                    duration_ns=elapsed_ns,
                    epoch_id=None,
                    trigger_reason=epoch_reason,
                )
            self.counters.allocator_call_records.append(call_record)

    def _plan_next_action(self, planner: AStarPlanner) -> StepResult:
        (
            plan_peer_positions,
            plan_collision_positions,
            plan_collision_intents,
        ) = self._promote_perception()

        # Admission can occur while a robot is already standing on the task
        # cell (including a cell traversed before release).  Service it as a
        # zero-motion visit after the admission epoch's allocator phase rather
        # than asking A* for a start==goal path and reporting path_failed.
        current_position_service = self._service_task_at_current_position()
        if current_position_service is not None:
            return current_position_service

        previous_task = self.current_goal
        previous_task_completed = previous_task is not None and previous_task not in self._active_tasks
        previous_task_invalidated = (
            (previous_task is not None and not previous_task_completed)
            or (previous_task is None and self.last_goal is not None and self.last_goal in self._active_tasks)
        )

        if self.current_goal is None or self.current_goal not in self._active_tasks:
            allocation_reason = self._next_allocation_reason
            if allocation_reason is None:
                if previous_task_completed:
                    allocation_reason = "task_completion"
                elif previous_task is not None:
                    allocation_reason = "invalid_goal"
                elif not self._active_tasks:
                    allocation_reason = "robot_idle"
                elif self.last_goal is None:
                    allocation_reason = "initial_allocation"
                else:
                    allocation_reason = "consensus/internal"
            self._next_allocation_reason = None
            self.current_goal = None
            self._active_peer_positions = plan_peer_positions
            try:
                decision = self._choose_goal_with_metrics(allocation_reason)
                # Absolute-time arrivals can open more than one eager epoch
                # before this robot wakes.  Drain every queued epoch so no
                # arrival-induced reallocation is silently overwritten.
                while (
                    self._reallocation_scheduler is not None
                    and self._reallocation_scheduler.has_queued_context(self.rid)
                ):
                    decision = self._choose_goal_with_metrics("consensus/internal")
            finally:
                self._active_peer_positions = None
                self.collision_avoidance_active = False
            if decision.goal is not None and decision.goal not in self._active_tasks:
                raise RuntimeError(
                    f"{self.allocator.name} selected inactive/non-target goal {decision.goal}"
                )
            self.current_goal = decision.goal
            if self.current_goal is None and self._active_tasks:
                if self._no_goal_since is None:
                    self._no_goal_since = self._now
                elif self._now - self._no_goal_since >= self.cfg.stalled_allocation_recovery_s:
                    recover = getattr(self.allocator, "recover_stalled_allocation", None)
                    if callable(recover) and recover(self):
                        self._stall_recovery_count += 1
                        self._no_goal_since = self._now
                        decision = self._choose_goal_with_metrics("stalled_recovery")
                        self.current_goal = decision.goal
            if self.current_goal is not None or not self._active_tasks:
                self._no_goal_since = None
            self.last_decision_debug = decision.debug
            self._record_owned_assignments()
            if self.current_goal is not None and self.current_goal != self.last_goal:
                if previous_task_invalidated:
                    self.counters.task_cell_replans += 1
                self.last_goal = self.current_goal
            self._publish_allocator_messages()

        if self.current_goal is None:
            self._set_collision_intent(None)
            self.last_event = "no_goal"
            return StepResult(reason="no_goal", time_cost_s=self.cfg.no_goal_delay_s)

        prior_temp_blocked_next = set(self.temp_blocked_next)
        blocked = set(plan_peer_positions.values())
        blocked.update(prior_temp_blocked_next)
        self.temp_blocked_next.clear()
        blocked.discard(self.pos)

        max_collision_replans = max(1, self.grid_size * self.grid_size)
        for _ in range(max_collision_replans):
            path = planner.plan(
                start=self.pos,
                heading=self.heading,
                goal=self.current_goal,
                target_p=self.target_p,
                searched=self._searched,
                blocked=blocked,
            )
            self.last_path = path
            if len(path) < 2:
                self.counters.path_replans += 1
                if self.current_goal is not None and self.current_goal in prior_temp_blocked_next:
                    backoff = self._maybe_temporarily_invalidate_blocked_goal(self.current_goal)
                    if backoff is not None:
                        return backoff
                self.current_goal = None
                self._notify_invalid_goal_epoch()
                self._set_collision_intent(None)
                self.last_event = "path_failed"
                return StepResult(reason="path_failed", time_cost_s=self.cfg.replan_delay_s)

            next_cell = path[1]
            if not self._collision_blocked_by(next_cell, plan_collision_positions, plan_collision_intents):
                self.temp_blocked_next.clear()
                self.last_next_cell = next_cell
                self._queue_actions_for_next_cell(next_cell)
                self._set_collision_intent(next_cell)
                return self._execute_pending_action(planner)

            self._record_collision_prevention(next_cell)
            backoff = self._maybe_temporarily_invalidate_blocked_goal(next_cell)
            if backoff is not None:
                return backoff
            blocked.add(next_cell)

        self.current_goal = None
        self._notify_invalid_goal_epoch()
        self._set_collision_intent(None)
        self.last_event = "path_failed"
        return StepResult(reason="path_failed", time_cost_s=self.cfg.replan_delay_s)

    def _queue_actions_for_next_cell(self, next_cell: Cell) -> None:
        self.pending_actions.clear()
        move_vec = (next_cell[0] - self.pos[0], next_cell[1] - self.pos[1])
        if move_vec not in DIRS4:
            raise ValueError(f"Next cell {next_cell} is not adjacent to {self.pos}")

        desired_heading = move_vec
        if self.heading not in DIRS4:
            self.pending_actions.append(PendingAction(kind="turn", target=next_cell, heading=desired_heading))
        elif self.heading != desired_heading:
            cur_idx = DIRS4.index(self.heading)
            desired_idx = DIRS4.index(desired_heading)
            cw_steps = (desired_idx - cur_idx) % len(DIRS4)
            ccw_steps = (cur_idx - desired_idx) % len(DIRS4)
            step = 1 if cw_steps <= ccw_steps else -1
            turns = min(cw_steps, ccw_steps)
            for _ in range(turns):
                cur_idx = (cur_idx + step) % len(DIRS4)
                self.pending_actions.append(
                    PendingAction(kind="turn", target=next_cell, heading=DIRS4[cur_idx])
                )

        if self.cfg.collision_intent_settle_s > 0:
            self.pending_actions.append(PendingAction(kind="intent_sync", target=next_cell, heading=desired_heading))
        self.pending_actions.append(PendingAction(kind="move", target=next_cell, heading=desired_heading))

    def _execute_pending_action(self, planner: Optional[AStarPlanner] = None) -> StepResult:
        if not self.pending_actions:
            self.last_next_cell = None
            return StepResult(reason="idle", time_cost_s=self.cfg.no_goal_delay_s)

        action = self.pending_actions.popleft()
        if action.target is not None:
            self.last_next_cell = action.target
        else:
            self.last_next_cell = None

        if action.kind == "turn":
            if action.heading is not None:
                self.heading = action.heading
            self.last_event = "turn"
            return StepResult(reason="turn", time_cost_s=self.cfg.turn_quarter_s)

        if action.kind == "intent_sync":
            self.last_event = "intent_sync"
            return StepResult(reason="intent_sync", time_cost_s=self.cfg.collision_intent_settle_s)

        if action.kind != "move" or action.target is None:
            self._clear_pending_actions()
            self._notify_invalid_goal_epoch()
            self.last_event = "path_failed"
            return StepResult(reason="path_failed", time_cost_s=self.cfg.replan_delay_s)

        return self._complete_move(action, planner)

    def _complete_move(self, action: PendingAction, planner: Optional[AStarPlanner] = None) -> StepResult:
        next_cell = action.target
        if next_cell is None:
            self._clear_pending_actions()
            self._notify_invalid_goal_epoch()
            self.last_event = "path_failed"
            return StepResult(reason="path_failed", time_cost_s=self.cfg.replan_delay_s)

        if self._collision_blocked(next_cell):
            self._clear_pending_actions()
            self._record_collision_prevention(next_cell)
            backoff = self._maybe_temporarily_invalidate_blocked_goal(next_cell)
            if backoff is not None:
                return backoff
            if planner is not None:
                return self._plan_next_action(planner)
            self.last_event = "path_failed"
            return StepResult(reason="path_failed", time_cost_s=self.cfg.replan_delay_s)

        return self._commit_move(action)

    def _commit_move(self, action: PendingAction) -> StepResult:
        """Apply an already-cleared traversal at its completion timestamp."""

        next_cell = action.target
        if next_cell is None:
            raise RuntimeError("cannot commit a move without a target cell")

        move_vec = (next_cell[0] - self.pos[0], next_cell[1] - self.pos[1])
        goal_before_move = self.current_goal
        old_pos = self.pos
        self.heading = action.heading or move_vec
        self.pos = next_cell
        if goal_before_move is not None:
            old_distance = abs(old_pos[0] - goal_before_move[0]) + abs(old_pos[1] - goal_before_move[1])
            new_distance = abs(self.pos[0] - goal_before_move[0]) + abs(self.pos[1] - goal_before_move[1])
            if new_distance < old_distance:
                self._blocked_goal_quarantine_level.pop(goal_before_move, None)
        self._collision_event_counted_since_move = False
        self._blocked_goal_failures.clear()
        self.counters.steps_total += 1
        self.publish_state()

        revisit = self.world.record_visit(self.rid, self.pos)
        if revisit:
            self.counters.system_revisits_by_robot += 1
        else:
            self.counters.unique_cells_contributed += 1
        self._searched.add(self.pos)

        target_visited, first_completion = self.world.record_target_visit(self.rid, self.pos, self._now)
        if target_visited:
            self._complete_task_locally(self.pos, reason="local_target_visit")
        if first_completion:
            self.counters.targets_found += 1
            if (
                self._reallocation_scheduler is not None
                and (
                    not self.world.all_targets_completed()
                    or self._reallocation_scheduler.pending_count > 0
                )
            ):
                self._reallocation_scheduler.mandatory_event(
                    self._now, "task_completion", source_robot_id=self.rid
                )
        elif target_visited:
            self.counters.task_cell_revisits += 1
        obs = Observation(
            time_s=self._now,
            cell=self.pos,
            searched=True,
            target_visited=target_visited,
            first_completion=first_completion,
        )
        self.allocator.on_observation(self, obs)
        self.last_event = "target_visited" if target_visited else "moved"
        self._capture_perception()
        self._publish_allocator_messages()
        self.pending_actions.clear()
        self.last_next_cell = None
        return StepResult(
            reason=self.last_event,
            moved=True,
            target_visited=target_visited,
            first_completion=first_completion,
            time_cost_s=self.cfg.async_step_mean_s,
            action_target=next_cell,
        )

    def _collision_blocked(self, cell: Cell) -> bool:
        return self._collision_blocked_by(cell, self._collision_peer_positions, self._collision_peer_intents)

    def _collision_blocked_by(
        self,
        cell: Cell,
        positions: Dict[str, Cell],
        intents: Dict[str, Optional[Cell]],
    ) -> bool:
        if cell in positions.values():
            return True
        if cell in [c for c in intents.values() if c is not None]:
            return True
        return False

    def _record_collision_prevention(self, cell: Cell) -> None:
        self.counters.path_replans += 1
        if not self._collision_event_counted_since_move:
            self.counters.collision_prevention_events += 1
            self._collision_event_counted_since_move = True
        self.temp_blocked_next.add(cell)
        self.collision_avoidance_active = True
        self.last_event = "collision_replan"

    def _maybe_temporarily_invalidate_blocked_goal(self, blocked_cell: Cell) -> Optional[StepResult]:
        goal = self.current_goal
        if goal is None or not self._allocation_active():
            return None

        self._blocked_goal_failures[goal] = self._blocked_goal_failures.get(goal, 0) + 1
        if self._blocked_goal_failures[goal] < 2:
            return None

        schedule = self.cfg.collision_goal_quarantine_schedule_s
        prior_level = int(self._blocked_goal_quarantine_level.get(goal, 0))
        level = min(prior_level, len(schedule) - 1)
        quarantine_s = float(schedule[level])
        self._blocked_goal_quarantine_level[goal] = min(level + 1, len(schedule) - 1)
        self._temporary_invalid_task_until[goal] = self._now + quarantine_s
        self.counters.blocked_task_quarantines += 1
        self.counters.blocked_task_quarantine_time_s += quarantine_s
        self.counters.maximum_quarantine_level = max(
            self.counters.maximum_quarantine_level, level + 1
        )
        self._blocked_goal_failures.pop(goal, None)
        self.current_goal = None
        self._next_allocation_reason = "invalid_goal"
        self._notify_invalid_goal_epoch()
        self._set_collision_intent(None)
        self.last_event = "blocked_goal_backoff"
        return StepResult(reason="blocked_goal_backoff", time_cost_s=self.cfg.replan_delay_s)

    def _expire_temporary_invalid_tasks(self) -> None:
        for cell, expires_at in list(self._temporary_invalid_task_until.items()):
            if self._now >= expires_at:
                self._temporary_invalid_task_until.pop(cell, None)

    def _allocation_active(self) -> bool:
        return True

    def _notify_invalid_goal_epoch(self) -> None:
        self._next_allocation_reason = "invalid_goal"
        if self._reallocation_scheduler is not None:
            self._reallocation_scheduler.mandatory_event(
                self._now, "invalid_goal", source_robot_id=self.rid
            )

    def _complete_task_locally(self, cell: Cell, reason: str) -> bool:
        if cell not in self._active_tasks:
            return False
        self._active_tasks.remove(cell)
        self._reported_assignments.discard(cell)
        self._blocked_goal_quarantine_level.pop(cell, None)
        self._temporary_invalid_task_until.pop(cell, None)
        self.last_event = reason
        self.current_goal = None
        self._next_allocation_reason = (
            "task_completion" if "target" in reason else "invalid_goal"
        )
        self._clear_pending_actions()
        handler = getattr(self.allocator, "on_task_set_changed", None)
        if callable(handler):
            handler(self)
        return True

    def _service_task_at_current_position(self) -> Optional[StepResult]:
        """Complete an admitted task underneath this robot without fake motion."""

        if self.pos not in self._active_tasks:
            return None
        record = self.world.target_records.get(self.pos)
        if record is None or record.admission_time_s is None:
            return None

        # Direct physical service is a legitimate assignment.  Preserve an
        # allocator-owned first assignment when one was already recorded.
        if record.first_assignment_time_s is None:
            self.world.record_assignment(self.rid, [self.pos], self._now)

        target_visited, first_completion = self.world.record_target_visit(
            self.rid, self.pos, self._now, completion_mode="stationary_service"
        )
        if not target_visited:
            return None
        self._complete_task_locally(self.pos, reason="local_target_visit")
        if first_completion:
            self.counters.targets_found += 1
            if (
                self._reallocation_scheduler is not None
                and (
                    not self.world.all_targets_completed()
                    or self._reallocation_scheduler.pending_count > 0
                )
            ):
                self._reallocation_scheduler.mandatory_event(
                    self._now, "task_completion", source_robot_id=self.rid
                )
        else:
            self.counters.task_cell_revisits += 1

        observation = Observation(
            time_s=self._now,
            cell=self.pos,
            searched=True,
            target_visited=True,
            first_completion=first_completion,
        )
        self.allocator.on_observation(self, observation)
        self.publish_state()
        self._capture_perception()
        self._publish_allocator_messages()
        self.last_event = "target_visited"
        return StepResult(
            reason="target_visited",
            moved=False,
            target_visited=True,
            first_completion=first_completion,
            time_cost_s=0.0,
        )

    def _record_owned_assignments(self) -> None:
        """Report allocator-owned execution paths using retained state names."""

        name = str(getattr(self.allocator, "name", "")).upper()
        attr_by_name = {
            "CBAA": "cbaa_current_task",
            "ACBBA": "acbba_path",
            "PI": "pi_path",
            "HIPC": "hipc_path",
            "DMCHBA": "dmchba_path",
            "DGA": "dga_path",
        }
        raw = getattr(self, attr_by_name.get(name, ""), None)
        if raw is None:
            values = [self.current_goal] if self.current_goal is not None else []
        elif isinstance(raw, tuple) and len(raw) == 2 and all(
            isinstance(value, int) for value in raw
        ):
            values = [raw]
        else:
            try:
                values = list(raw)
            except TypeError:
                values = []
        owned: Set[Cell] = set()
        for value in values:
            try:
                cell = (int(value[0]), int(value[1]))
            except (TypeError, ValueError, IndexError):
                continue
            if cell in self._active_tasks:
                owned.add(cell)
        if self.current_goal is not None and self.current_goal in self._active_tasks:
            owned.add(self.current_goal)
        newly_assigned = sorted(owned - self._reported_assignments)
        if newly_assigned:
            self.world.record_assignment(self.rid, newly_assigned, self._now)
        self._reported_assignments = owned

    def _clear_pending_actions(self) -> None:
        self.pending_actions.clear()
        self.last_next_cell = None

    def _publish_allocator_messages(self) -> None:
        for payload in self._allocator_outbound_payloads():
            if not isinstance(payload, dict):
                continue
            category = payload.get("type")
            if not isinstance(category, str) or not category:
                continue
            self._publish(category, payload)

    def _notify_allocator_collision_avoidance(self) -> None:
        handler = getattr(self.allocator, "on_collision_avoidance_activated", None)
        if callable(handler) and handler(self) is not False:
            self.current_goal = None

    def _allocator_outbound_payloads(self) -> List[Dict[str, Any]]:
        for method_name in (
            "make_messages",
            "get_outbound_messages",
            "build_dga_messages",
            "build_acbba_messages",
            "build_cbaa_messages",
            "make_message",
            "get_outbound_message",
            "build_dga_message",
            "build_acbba_message",
            "build_cbaa_message",
        ):
            method = getattr(self.allocator, method_name, None)
            if not callable(method):
                continue
            payloads = method(self)
            if payloads is None:
                return []
            if isinstance(payloads, dict):
                return [payloads]
            return [payload for payload in payloads if isinstance(payload, dict)]
        return []

    def _deliver_allocator_payload(self, payload: Dict[str, Any]) -> None:
        category = payload.get("type")
        cell = _payload_cell((payload.get("x"), payload.get("y")))
        if cell is not None and cell not in self._active_tasks and not payload.get("removed", False):
            return
        for receiver_name in ("receive_message", "on_message", "process_message"):
            receiver = getattr(self.allocator, receiver_name, None)
            if callable(receiver):
                self._causal_device_events.append({
                    "kind": "allocator_message",
                    "payload": copy.deepcopy(payload),
                })
                receiver(self, payload)
                return
        handler_names = (
            ("handle_acbba_message", "handle_cbaa_message")
            if category == "acbba_entry"
            else ("handle_cbaa_message", "handle_acbba_message")
        )
        for handler_name in handler_names:
            handler = getattr(self.allocator, handler_name, None)
            if callable(handler):
                self._causal_device_events.append({
                    "kind": "allocator_message",
                    "payload": copy.deepcopy(payload),
                })
                handler(self, payload)
                return

    def _capture_perception(self) -> None:
        self._perception_pending_positions = dict(self._peer_positions)
        self._perception_pending_collision_positions = dict(self._collision_peer_positions)
        self._perception_pending_collision_intents = dict(self._collision_peer_intents)
        self._perception_pending_valid = True

    def _promote_perception(
        self,
    ) -> Tuple[
        Dict[str, Cell],
        Dict[str, Cell],
        Dict[str, Optional[Cell]],
    ]:
        if not self._perception_initialized and not self._perception_pending_valid:
            self._capture_perception()
        if self._perception_pending_valid:
            positions = dict(self._perception_pending_positions)
            collision_positions = dict(self._perception_pending_collision_positions)
            collision_intents = dict(self._perception_pending_collision_intents)
            self._perception_pending_valid = False
            self._perception_initialized = True
            return positions, collision_positions, collision_intents
        return (
            dict(self._peer_positions),
            dict(self._collision_peer_positions),
            dict(self._collision_peer_intents),
        )


def _payload_cell(raw: Any) -> Optional[Cell]:
    if raw is None:
        return None
    try:
        return (int(raw[0]), int(raw[1]))
    except (TypeError, ValueError, IndexError):
        return None
