from __future__ import annotations

import heapq
import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Type, Union

from known_visit_sim.algorithms.base import AllocatorBase
from known_visit_sim.comms.bus import MessageBus
from known_visit_sim.comms.models import CommunicationModel
from known_visit_sim.config import SimConfig
from .planner import AStarPlanner
from .reallocation import (
    OnlineReallocationScheduler,
    ReallocationPolicy,
    ReleaseTimes,
    TaskState,
    build_online_metrics,
    normalize_release_times,
)
from .robot import RobotShell, StepResult
from .timing import (
    CausalTimingProvider,
    FrozenAllocatorCall,
    MeasuredAllocatorCall,
    MissionTimingBinding,
    ZeroComputeTimingProvider,
    replay_encode_value,
    validate_and_index_measurements,
)
from .types import TrialScenario
from .world import World


MOVEMENT_TIMING_MODEL = "sha256_keyed_uniform_jitter_v1"


def exogenous_movement_duration_s(
    cfg: SimConfig,
    *,
    runtime_seed: int,
    trace_id: str,
    robot_id: str,
    robot_action_index: int,
    source_cell: tuple[int, int],
    target_cell: tuple[int, int],
) -> tuple[float, str]:
    """Return an event-order-independent movement duration and sample key.

    A hash-derived uniform variate replaces consumption of the simulator's
    shared PRNG stream.  The same robot action on the same paired trace and
    edge therefore keeps its movement jitter when allocator durations or
    reallocation policies change event interleaving.
    """

    key = {
        "model": MOVEMENT_TIMING_MODEL,
        "runtime_seed": int(runtime_seed),
        "trace_id": str(trace_id),
        "robot_id": str(robot_id),
        "robot_action_index": int(robot_action_index),
        "source_cell": [int(source_cell[0]), int(source_cell[1])],
        "target_cell": [int(target_cell[0]), int(target_cell[1])],
    }
    digest = hashlib.sha256(
        json.dumps(key, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()
    unit = (int.from_bytes(digest[:8], "big") + 0.5) / float(1 << 64)
    jitter = (2.0 * unit - 1.0) * float(cfg.async_step_jitter_s)
    duration_s = max(
        1e-3,
        min(
            cfg.async_max_delay_s,
            max(cfg.async_min_delay_s, cfg.async_step_mean_s + jitter),
        ),
    )
    return float(duration_s), digest.hex()


@dataclass(order=True)
class WakeEvent:
    time_s: float
    order: int
    rid: str = field(compare=False)


@dataclass(order=True)
class OnlineEvent:
    time_s: float
    priority: int
    order: int
    kind: str = field(compare=False)
    rid: Optional[str] = field(default=None, compare=False)
    token: int = field(default=0, compare=False)


@dataclass
class MovementRecord:
    movement_id: int
    robot_id: str
    start_time_s: float
    completion_time_s: float
    source_cell: tuple[int, int]
    target_cell: tuple[int, int]
    robot_action_index: int = 0
    timing_key_sha256: str = ""
    timing_model: str = MOVEMENT_TIMING_MODEL
    first_task_completion: bool = False
    completed_task_id: Optional[Any] = None

    @property
    def duration_s(self) -> float:
        return self.completion_time_s - self.start_time_s

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "duration_s": self.duration_s,
        }


@dataclass
class TrialState:
    cfg: SimConfig
    scenario: TrialScenario
    world: World
    robots: Dict[str, RobotShell]
    bus: MessageBus
    planner: AStarPlanner
    clock_s: float = 0.0
    events_processed: int = 0
    scheduler_events_processed: int = 0
    done: bool = False
    reallocation_scheduler: Optional[OnlineReallocationScheduler] = None
    host_program_runtime_s: float = 0.0
    event_rng: Optional[random.Random] = field(default=None, repr=False)
    movement_records: List[MovementRecord] = field(default_factory=list)
    compute_group_count: int = 0
    timing_provider_name: str = ""
    causal_timing_enabled: bool = False
    movement_timing_model: str = MOVEMENT_TIMING_MODEL
    movement_timing_seed: int = 0
    movement_timing_trace_id: str = ""
    causal_event_horizon_events: int = 0
    causal_stagnation_horizon_events: int = 0
    algorithmic_failure_type: Optional[str] = None
    algorithmic_failure_detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def simulated_execution_time_s(self) -> float:
        return float(self.clock_s)

    @property
    def cumulative_allocator_time_s(self) -> float:
        calls = self.allocator_call_rows()
        return sum(
            float(call.get("device_allocator_duration_s", call.get("duration_s", 0.0)))
            for call in calls
            if bool(call.get("valid_for_mission", True))
        )

    @property
    def cumulative_agx_allocator_time_s(self) -> float:
        return sum(
            call.agx_allocator_duration_s
            for call in (
                self.reallocation_scheduler.allocator_calls
                if self.reallocation_scheduler is not None else []
            )
            if call.valid_for_mission
        )

    @property
    def allocator_parallel_critical_path_time_s(self) -> float:
        from .reallocation import allocator_parallel_critical_path_s

        scheduler = self.reallocation_scheduler
        calls = scheduler.allocator_calls if scheduler is not None else []
        return allocator_parallel_critical_path_s(calls)

    @property
    def mission_elapsed_time_s(self) -> float:
        completions = [
            record.first_completion_time_s
            for record in self.world.target_records.values()
            if record.first_completion_time_s is not None
        ]
        if self.world.all_targets_completed() and completions:
            return max(completions)
        return float(self.clock_s)

    def online_metrics(self) -> dict:
        return build_online_metrics(self)

    def task_rows(self) -> list[dict]:
        return self.world.task_rows()

    def epoch_rows(self) -> list[dict]:
        scheduler = self.reallocation_scheduler
        return [epoch.to_dict() for epoch in scheduler.epochs] if scheduler else []

    def allocator_call_rows(self) -> list[dict]:
        scheduler = self.reallocation_scheduler
        if scheduler:
            return [call.to_dict() for call in scheduler.allocator_calls]
        return [
            call.to_dict()
            for robot in self.robots.values()
            for call in robot.counters.allocator_call_records
        ]

    def queue_sample_rows(self) -> list[dict]:
        scheduler = self.reallocation_scheduler
        return [sample.to_dict() for sample in scheduler.queue_samples] if scheduler else []

    def movement_rows(self) -> list[dict]:
        return [record.to_dict() for record in self.movement_records]

    def serializable_online_result(self) -> dict:
        scheduler = self.reallocation_scheduler
        return {
            "summary": self.online_metrics(),
            "policy": scheduler.policy.to_dict() if scheduler else None,
            "tasks": self.task_rows(),
            "allocation_epochs": self.epoch_rows(),
            "allocator_calls": self.allocator_call_rows(),
            "pending_queue_samples": self.queue_sample_rows(),
            "movements": self.movement_rows(),
        }

    def validate_online_invariants(self) -> None:
        if self.world.all_targets_completed():
            final_completion = max(
                record.first_completion_time_s
                for record in self.world.target_records.values()
                if record.first_completion_time_s is not None
            )
            if abs(self.mission_elapsed_time_s - final_completion) > 1e-9:
                raise AssertionError("mission elapsed must equal final task completion")
            if abs(self.clock_s - final_completion) > 1e-9:
                raise AssertionError("completed mission clock must stop at final completion")
        for record in self.world.target_records.values():
            timestamps = [
                record.released_time_s,
                record.admission_time_s,
                record.first_eligible_allocator_start_time_s,
                record.first_assignment_time_s,
                record.first_completion_time_s,
            ]
            observed = [value for value in timestamps if value is not None]
            if observed != sorted(observed):
                raise AssertionError(f"task timestamp order invalid for {record.task_id}")
            if record.completed and record.first_assignment_time_s is None:
                raise AssertionError(f"completed task {record.task_id} lacks an assignment")
        for movement in self.movement_records:
            if movement.completion_time_s <= movement.start_time_s:
                raise AssertionError("movement completion must follow movement start")


class AsyncTrialRunner:
    def __init__(self, cfg: SimConfig, allocator_cls: Type[AllocatorBase],
                 comm_model: CommunicationModel, seed: int = 0,
                 timing_provider: Optional[CausalTimingProvider] = None) -> None:
        self.cfg = cfg
        self.allocator_cls = allocator_cls
        self.comm_model = comm_model
        self.seed = int(seed)
        self.rng = random.Random(seed)
        self.timing_provider = timing_provider

    def new_trial(self, scenario: TrialScenario) -> TrialState:
        bus = MessageBus(self.comm_model, self.cfg.comm_delay_s, self.cfg.comm_delay_jitter_s, self.rng)
        world = World(self.cfg.grid_size, scenario)
        planner = AStarPlanner(
            self.cfg.grid_size, self.cfg.move_cost, self.cfg.turn_cost,
            self.cfg.visited_step_penalty, self.cfg.reward_factor, self.cfg.min_step_cost,
        )
        robots = {
            rid: RobotShell(
                rid, self.cfg.start_positions[rid], self.cfg.start_headings[rid],
                self.cfg, world, bus, self.allocator_cls(),
            )
            for rid in self.cfg.robot_ids
        }
        # Registration must finish before initial state is broadcast so every
        # peer has the same opportunity (subject to the communication model)
        # to learn each starting location.
        for robot in robots.values():
            robot.publish_state()
        return TrialState(self.cfg, scenario, world, robots, bus, planner)

    def new_online_trial(
        self,
        scenario: TrialScenario,
        release_times: ReleaseTimes,
        policy: Union[ReallocationPolicy, str, Mapping[str, Any]],
    ) -> TrialState:
        normalized = normalize_release_times(scenario, release_times)
        trace_seed_material = (
            self.seed,
            scenario.trial_id,
            tuple((cell, normalized[cell]) for cell in scenario.targets),
        )
        event_seed = int.from_bytes(
            hashlib.sha256(repr(("events", trace_seed_material)).encode("utf-8")).digest()[:8],
            "big",
        )
        comm_seed = int.from_bytes(
            hashlib.sha256(repr(("communications", trace_seed_material)).encode("utf-8")).digest()[:8],
            "big",
        )
        bus = MessageBus(
            self.comm_model,
            self.cfg.comm_delay_s,
            self.cfg.comm_delay_jitter_s,
            random.Random(comm_seed),
        )
        world = World(self.cfg.grid_size, scenario)
        world.configure_release_times(normalized)
        planner = AStarPlanner(
            self.cfg.grid_size,
            self.cfg.move_cost,
            self.cfg.turn_cost,
            self.cfg.visited_step_penalty,
            self.cfg.reward_factor,
            self.cfg.min_step_cost,
        )
        robots = {
            rid: RobotShell(
                rid,
                self.cfg.start_positions[rid],
                self.cfg.start_headings[rid],
                self.cfg,
                world,
                bus,
                self.allocator_cls(),
            )
            for rid in self.cfg.robot_ids
        }
        for robot in robots.values():
            # Consumed only by online-aware stochastic allocators.  Static
            # trials retain their exact legacy per-robot seed behavior.
            robot._online_trace_seed = trace_seed_material
        for robot in robots.values():
            robot.publish_state()
        coordinator = OnlineReallocationScheduler(
            world, ReallocationPolicy.from_spec(policy)
        )
        coordinator.attach_robots(robots)
        return TrialState(
            self.cfg,
            scenario,
            world,
            robots,
            bus,
            planner,
            reallocation_scheduler=coordinator,
            event_rng=random.Random(event_seed),
        )

    def initial_queue(self, state: TrialState) -> List[WakeEvent]:
        queue: List[WakeEvent] = []
        rng = state.event_rng or self.rng
        span = self.cfg.async_step_mean_s * max(0.0, self.cfg.async_initial_spread_s)
        for index, rid in enumerate(state.robots):
            heapq.heappush(queue, WakeEvent(rng.uniform(0.0, span) if span else 0.0, index, rid))
        return queue

    def run_trial(self, scenario: TrialScenario,
                  on_step: Optional[Callable[[TrialState, RobotShell, StepResult], None]] = None) -> TrialState:
        state = self.new_trial(scenario)
        queue = self.initial_queue(state)
        order = len(queue)
        last_progress = self._progress_signature(state)
        stagnant_events = 0
        reasons: Counter[str] = Counter()
        while queue and not state.done:
            event = heapq.heappop(queue)
            state.clock_s = event.time_s
            state.bus.pump(state.clock_s)
            robot = state.robots[event.rid]
            result = robot.step(state.clock_s, state.planner)
            state.events_processed += 1
            reasons[result.reason] += 1
            if on_step:
                on_step(state, robot, result)
            if state.world.all_targets_completed():
                state.done = True
                break
            progress = self._progress_signature(state)
            if progress == last_progress:
                stagnant_events += 1
            else:
                last_progress = progress
                stagnant_events = 0
            if stagnant_events >= self.cfg.debug_max_stagnant_events:
                raise RuntimeError(
                    f"Stagnation detected in trial {scenario.trial_id} after "
                    f"{stagnant_events} events without movement or target completion; "
                    f"diagnostics={json.dumps(self._diagnostics(state, reasons), sort_keys=True, default=str)}"
                )
            if state.events_processed >= self.cfg.debug_max_events:
                raise RuntimeError(
                    f"Debug safety cap reached in trial {scenario.trial_id}; "
                    f"diagnostics={json.dumps(self._diagnostics(state, reasons), sort_keys=True, default=str)}"
                )
            order += 1
            heapq.heappush(queue, WakeEvent(state.clock_s + self._interval_for(result), order, event.rid))
        return state

    def run_online_trial(
        self,
        scenario: TrialScenario,
        release_times: ReleaseTimes,
        policy: Union[ReallocationPolicy, str, Mapping[str, Any]],
        on_step: Optional[Callable[[TrialState, RobotShell, StepResult], None]] = None,
        timing_provider: Optional[CausalTimingProvider] = None,
    ) -> TrialState:
        """Run one causal online mission with independent robot processors.

        Authoritative allocator calls execute synchronously on the host only
        after every member of a same-time group has frozen its pre-call view.
        The timing provider may physically sample those calls serially; each
        logical completion is nevertheless scheduled at the common virtual
        start plus that robot's own device duration.
        """

        host_started = perf_counter()
        state = self.new_online_trial(scenario, release_times, policy)
        state.causal_timing_enabled = True
        scheduler = state.reallocation_scheduler
        if scheduler is None:  # pragma: no cover - construction invariant
            raise AssertionError("online trial requires a reallocation scheduler")
        # An unbound software run is the deterministic zero-compute
        # counterfactual. Hardware/AGX-proxy durations must be selected
        # explicitly so a development run cannot masquerade as RP2040-timed.
        provider = timing_provider or self.timing_provider or ZeroComputeTimingProvider()
        state.timing_provider_name = provider.__class__.__name__
        trial_label = str(scenario.metadata.get("trace_id", scenario.trial_id))
        algorithm = str(getattr(self.allocator_cls, "name", self.allocator_cls.__name__)).upper()
        initial_states = {
            rid: robot.causal_allocator_snapshot()
            for rid, robot in state.robots.items()
        }
        trial_config = self.cfg.to_dict()
        trial_config.update({
            "mission": "known_visit_online",
            "algorithm": algorithm,
            "seed": int(self.seed),
            # The native resident context validates delayed admissions against
            # this immutable universe.  Only already admitted release-zero
            # tasks belong in the initial active set.
            "all_tasks": [list(cell) for cell in scenario.targets],
            "active_tasks": [list(cell) for cell in sorted(state.world.admitted_targets)],
            "logical_context_count": len(state.robots),
        })
        binding = MissionTimingBinding(
            trial_id=trial_label,
            condition_id=self.cfg.condition_id or f"trial-{trial_label}",
            algorithm=algorithm,
            seed=self.seed,
            robot_ids=tuple(state.robots),
            trial_config=trial_config,
            initial_context_states=initial_states,
        )
        movement_trace_id = str(
            scenario.metadata.get(
                "trace_id", scenario.metadata.get("release_trace_id", scenario.trial_id)
            )
        )
        state.movement_timing_model = MOVEMENT_TIMING_MODEL
        state.movement_timing_seed = int(self.seed)
        state.movement_timing_trace_id = movement_trace_id

        queue: List[OnlineEvent] = []
        order = 0
        robot_tokens = {rid: 0 for rid in state.robots}
        staged_by_call: Dict[
            str, tuple[RobotShell, Any, MeasuredAllocatorCall]
        ] = {}
        inflight_movements: Dict[str, MovementRecord] = {}
        scheduled_timeouts: set[float] = set()
        call_sequence = 0
        group_sequence = 0
        movement_action_counts = {rid: 0 for rid in state.robots}

        def schedule_robot(kind: str, time_s: float, rid: str, priority: int = 3) -> None:
            nonlocal order
            robot_tokens[rid] += 1
            heapq.heappush(
                queue,
                OnlineEvent(
                    float(time_s), priority, order, kind, rid, robot_tokens[rid]
                ),
            )
            order += 1

        for wake in self.initial_queue(state):
            schedule_robot("control", wake.time_s, wake.rid, 3)
        release_times_unique = sorted({
            record.release_time_s
            for record in state.world.target_records.values()
            if record.release_time_s > 0.0
        })
        for release_s in release_times_unique:
            heapq.heappush(queue, OnlineEvent(release_s, 0, order, "release"))
            order += 1

        def schedule_timeout() -> None:
            nonlocal order
            deadline = scheduler.next_timeout_s()
            if deadline is None:
                return
            key = round(float(deadline), 12)
            if key in scheduled_timeouts:
                return
            scheduled_timeouts.add(key)
            heapq.heappush(queue, OnlineEvent(float(deadline), 1, order, "timeout"))
            order += 1

        def ready_queued_robots(ready: set[str]) -> None:
            for rid, robot in state.robots.items():
                if not robot.causal_is_busy and scheduler.has_queued_context(rid):
                    robot_tokens[rid] += 1  # invalidate a later idle/control wake
                    ready.add(rid)

        def schedule_control_result(
            rid: str, robot: RobotShell, result: StepResult, now_s: float
        ) -> bool:
            """Schedule the result; return True for an instantaneous retry."""

            nonlocal order
            state.events_processed += 1
            if on_step:
                on_step(state, robot, result)
            if result.reason == "allocation_required":
                return True
            if result.reason == "move_started":
                if result.action_target is None:
                    raise AssertionError("move start lacks a target")
                action_index = movement_action_counts[rid]
                movement_action_counts[rid] += 1
                duration_s, timing_key = exogenous_movement_duration_s(
                    self.cfg,
                    runtime_seed=self.seed,
                    trace_id=movement_trace_id,
                    robot_id=rid,
                    robot_action_index=action_index,
                    source_cell=robot.pos,
                    target_cell=result.action_target,
                )
                movement = MovementRecord(
                    movement_id=len(state.movement_records) + 1,
                    robot_id=rid,
                    start_time_s=now_s,
                    completion_time_s=now_s + duration_s,
                    source_cell=robot.pos,
                    target_cell=result.action_target,
                    robot_action_index=action_index,
                    timing_key_sha256=timing_key,
                )
                state.movement_records.append(movement)
                inflight_movements[rid] = movement
                schedule_robot("move_complete", movement.completion_time_s, rid, 2)
                return False
            delay = self._interval_for(result, state.event_rng)
            next_control = now_s + delay
            if (
                result.reason in {"no_goal", "idle"}
                and not any(item.active_tasks for item in state.robots.values())
                and scheduler.pending_count == 0
                and scheduler.unreleased_count > 0
            ):
                next_release = min(
                    record.release_time_s
                    for record in state.world.target_records.values()
                    if record.state == TaskState.UNRELEASED
                )
                next_control = max(next_control, next_release)
            schedule_robot("control", next_control, rid, 3)
            return False

        def execute_call_group(now_s: float, ready: set[str]) -> None:
            nonlocal call_sequence, group_sequence
            # A newly idle robot can turn a below-threshold pending tail into
            # a mandatory global admission. Resolve that shared scheduler
            # transition before freezing *any* member of this same-time
            # compute group, so no peer receives a pre-admission snapshot at
            # the identical virtual start.
            if scheduler.pending_count:
                idle_trigger = next((
                    rid
                    for rid in sorted(ready)
                    if not state.robots[rid].causal_is_busy
                    and state.robots[rid].causal_allocation_reason() == "robot_idle"
                ), None)
                if idle_trigger is not None:
                    scheduler.mandatory_event(
                        now_s, "robot_idle", source_robot_id=idle_trigger
                    )
                    ready_queued_robots(ready)
            prepared: Dict[str, Any] = {}
            # Acquiring a robot-idle context can piggyback pending admissions
            # and queue peers.  Expand the group before any authoritative call.
            while True:
                additions = [
                    rid for rid in sorted(ready)
                    if rid not in prepared
                    and not state.robots[rid].causal_is_busy
                    and state.robots[rid].causal_allocation_reason() is not None
                ]
                if not additions:
                    break
                for rid in additions:
                    robot = state.robots[rid]
                    reason = robot.causal_allocation_reason()
                    if reason is None:
                        continue
                    prepared[rid] = robot.prepare_causal_allocation(now_s, reason)
                ready_queued_robots(ready)
            if not prepared:
                return

            group_sequence += 1
            state.compute_group_count += 1
            group_id = f"{trial_label}/group-{group_sequence:06d}"
            staged: Dict[str, Any] = {}
            frozen_calls: List[FrozenAllocatorCall] = []
            # All pre-call snapshots above are complete before the first call
            # below mutates even its own private allocator context.
            for rid in sorted(prepared):
                robot = state.robots[rid]
                item = robot.stage_causal_allocation(prepared[rid])
                staged[rid] = item
                call_sequence += 1
                call_id = f"{trial_label}/call-{call_sequence:07d}"
                fixture_id = f"{call_id}/setup"
                agx_choose_goal_us = max(
                    0,
                    int(round(item.agx_choose_goal_duration_ns / 1_000.0)),
                )
                agx_epoch_reset_us = max(
                    0,
                    int(round(
                        item.agx_algorithm_epoch_reset_duration_ns / 1_000.0
                    )),
                )
                # Component-wise conversion preserves the public microsecond
                # decomposition exactly even when each ns timer sample rounds.
                agx_allocator_time_us = (
                    agx_choose_goal_us + agx_epoch_reset_us
                )
                # The shell records allocator messages and admission callbacks
                # at the instant they are actually applied.  Draining that
                # unified log preserves their exact relative order, including
                # admissions buffered during an in-flight compute interval.
                device_events: List[Dict[str, Any]] = [
                    {
                        "kind": str(event["kind"]),
                        "payload": replay_encode_value(event.get("payload", {})),
                    }
                    for event in item.prepared.device_events
                ]
                device_setup = {
                    "schema": 1,
                    "fixture_id": fixture_id,
                    "condition_id": binding.condition_id,
                    "mission": "known_visit_online",
                    "algorithm": algorithm,
                    "context_id": rid,
                    "setup_mode": "restore",
                    "deleted": {},
                    "events": device_events,
                    "resume_state": {},
                    "pre_state": item.prepared.pre_state,
                }
                frozen_calls.append(FrozenAllocatorCall(
                    call_id=call_id,
                    group_id=group_id,
                    trial_id=trial_label,
                    logical_robot_id=rid,
                    algorithm=algorithm,
                    virtual_start_s=now_s,
                    device_setup=device_setup,
                    authoritative=item.signature,
                    agx_allocator_time_us=agx_allocator_time_us,
                    trigger_id=(
                        str(item.prepared.epoch_id)
                        if item.prepared.epoch_id is not None else ""
                    ),
                    active_task_count=len(item.prepared.active_tasks_at_start),
                    agx_choose_goal_us=agx_choose_goal_us,
                    agx_algorithm_epoch_reset_us=agx_epoch_reset_us,
                    metadata={
                        "epoch_id": item.prepared.epoch_id,
                        "trigger_reason": item.prepared.trigger_reason,
                        "pre_state": item.prepared.pre_state,
                        "pre_state_sha256": item.prepared.pre_state_sha256,
                        "authoritative_messages": item.outbound_payloads,
                        "authoritative_post_state": item.post_state,
                        "candidate_count": item.signature.active_candidate_count,
                        "pending_task_count": scheduler.pending_count,
                        "current_position": tuple(robot.pos),
                        "device_events": device_events,
                        "device_deleted": {},
                        "device_resume_state": {},
                        "agx_choose_goal_us": agx_choose_goal_us,
                        "agx_algorithm_epoch_reset_us": agx_epoch_reset_us,
                    },
                ))
            detached = tuple(call.detached_copy() for call in frozen_calls)
            raw_measurements = provider.measure_group(detached)
            measurements = validate_and_index_measurements(
                frozen_calls, tuple(raw_measurements)
            )
            by_robot = {call.logical_robot_id: call for call in frozen_calls}
            for rid in sorted(prepared):
                robot = state.robots[rid]
                frozen = by_robot[rid]
                measured = measurements[frozen.call_id]
                item = staged[rid]
                duration_ns = measured.device_allocator_time_us * 1_000
                call_record = scheduler.record_allocator_call(
                    rid,
                    now_s,
                    duration_ns,
                    item.prepared.epoch_id,
                    item.prepared.trigger_reason,
                    group_id=group_id,
                    provider_call_id=frozen.call_id,
                    logical_context_id=measured.context_id or rid,
                    compute_start_time_s=now_s,
                    compute_completion_time_s=measured.virtual_completion_s,
                    agx_allocator_duration_ns=item.agx_duration_ns,
                    agx_choose_goal_duration_ns=(
                        item.agx_choose_goal_duration_ns
                    ),
                    agx_algorithm_epoch_reset_duration_ns=(
                        item.agx_algorithm_epoch_reset_duration_ns
                    ),
                    device_allocator_duration_ns=duration_ns,
                    device_choose_goal_duration_ns=(
                        measured.device_choose_goal_us * 1_000
                    ),
                    algorithm_epoch_reset_duration_ns=(
                        measured.algorithm_epoch_reset_us * 1_000
                    ),
                    serial_roundtrip_ns=measured.serial_roundtrip_us * 1_000,
                    host_serialization_setup_ns=(
                        measured.host_serialization_setup_us * 1_000
                    ),
                    host_total_call_ns=measured.host_total_call_us * 1_000,
                    psetup_transaction_ns=(
                        measured.psetup_transaction_us * 1_000
                    ),
                    device_pre_call_setup_ns=(
                        None
                        if measured.device_pre_call_setup_us is None
                        else measured.device_pre_call_setup_us * 1_000
                    ),
                    ptime_result_transaction_ns=(
                        measured.ptime_result_transaction_us * 1_000
                    ),
                    host_prepare_cpu_ns=measured.host_prepare_cpu_us * 1_000,
                    timing_decomposition_schema=(
                        measured.timing_decomposition_schema
                    ),
                    host_serialization_setup_measured=(
                        measured.host_serialization_setup_measured
                    ),
                    device_allocator_timer_scope=(
                        measured.device_allocator_timer_scope
                    ),
                    serial_roundtrip_definition=(
                        measured.serial_roundtrip_definition
                    ),
                    timing_source=measured.timing_source,
                    parity_passed=True,
                    valid_for_mission=True,
                    active_task_count=len(item.prepared.active_tasks_at_start),
                    candidate_count=item.signature.active_candidate_count,
                    current_position=tuple(robot.pos),
                    authoritative_goal=item.signature.goal,
                    outbound_message_sha256=item.signature.message_sha256,
                    pre_state_sha256=item.prepared.pre_state_sha256,
                    authoritative_post_state_sha256=item.signature.post_state_sha256,
                    device_goal=measured.device_goal,
                    device_message_sha256=measured.device_message_sha256,
                    device_post_state_sha256=measured.device_post_state_sha256,
                    call_class=item.signature.call_class,
                    board_id=measured.board_id,
                    serial_device=measured.serial_device,
                    attempt_id=measured.attempt_id,
                    physical_measurement_index=measured.physical_measurement_index,
                    hardware_validated=measured.hardware_validated,
                    provider_metadata=dict(measured.metadata),
                )
                robot.counters.allocator_time_ns_samples.append(duration_ns)
                robot.counters.allocator_call_records.append(call_record)
                staged_by_call[frozen.call_id] = (robot, item, measured)
                schedule_robot(
                    f"compute_complete:{frozen.call_id}",
                    measured.virtual_completion_s,
                    rid,
                    2,
                )
                ready.discard(rid)

        last_progress = self._progress_signature(state)
        stagnant_events = 0
        reasons: Counter[str] = Counter()
        default_event_horizon = max(
            self.cfg.debug_max_events,
            1_000 + 5_000 * len(state.world.target_records),
        )
        default_stagnation_horizon = max(
            self.cfg.debug_max_stagnant_events,
            500 + 100 * len(state.world.target_records),
        )
        event_horizon = int(
            scenario.metadata.get(
                "causal_event_horizon_events", default_event_horizon
            )
        )
        stagnation_horizon = int(
            scenario.metadata.get(
                "causal_stagnation_horizon_events",
                default_stagnation_horizon,
            )
        )
        if event_horizon <= 0 or stagnation_horizon <= 0:
            raise ValueError("causal event/stagnation horizons must be positive")
        state.causal_event_horizon_events = event_horizon
        state.causal_stagnation_horizon_events = stagnation_horizon
        began = False
        primary_failure: BaseException | None = None
        try:
            provider.begin_mission(binding)
            began = True
            schedule_timeout()
            while queue and not state.done:
                now_s = queue[0].time_s
                state.clock_s = now_s
                batch: List[OnlineEvent] = []
                while queue and abs(queue[0].time_s - now_s) <= 1e-12:
                    batch.append(heapq.heappop(queue))
                ready: set[str] = set()
                for event in sorted(batch):
                    if event.rid is not None and event.token != robot_tokens[event.rid]:
                        continue
                    if event.kind == "release":
                        scheduler.release_due(now_s)
                        state.scheduler_events_processed += 1
                        schedule_timeout()
                        continue
                    if event.kind == "timeout":
                        scheduler.timeout_due(now_s)
                        state.scheduler_events_processed += 1
                        schedule_timeout()
                        continue
                    if event.rid is None:
                        raise AssertionError("robot event lacks a robot ID")
                    robot = state.robots[event.rid]
                    if event.kind == "move_complete":
                        result = robot.complete_causal_move(now_s)
                        movement = inflight_movements.pop(event.rid)
                        movement.first_task_completion = result.first_completion
                        if result.first_completion:
                            target = state.world.target_records.get(movement.target_cell)
                            movement.completed_task_id = target.task_id if target else None
                        state.events_processed += 1
                        reasons[result.reason] += 1
                        if on_step:
                            on_step(state, robot, result)
                        ready.add(event.rid)
                        continue
                    if event.kind.startswith("compute_complete:"):
                        call_id = event.kind.split(":", 1)[1]
                        staged_robot, staged, _ = staged_by_call.pop(call_id)
                        if staged_robot is not robot:
                            raise AssertionError("compute completion robot mismatch")
                        result = robot.complete_causal_allocation(now_s, staged)
                        state.events_processed += 1
                        reasons[result.reason] += 1
                        if on_step:
                            on_step(state, robot, result)
                        ready.add(event.rid)
                        continue
                    if event.kind == "control":
                        ready.add(event.rid)
                        continue
                    raise AssertionError(f"unknown online event {event.kind}")

                # Deliver every message whose communication delay expires at
                # this timestamp. Computing receivers buffer it; idle receivers
                # may use it in a call that starts now.
                state.bus.pump(now_s)
                ready_queued_robots(ready)
                if state.world.all_targets_completed():
                    state.done = True
                    state.clock_s = state.mission_elapsed_time_s
                    break

                instant_loops = 0
                while ready and not state.done:
                    instant_loops += 1
                    if instant_loops > 10_000:
                        raise RuntimeError("same-time causal control loop did not quiesce")
                    execute_call_group(now_s, ready)
                    # Calls have now become busy. Advance remaining idle robots
                    # by at most one control action before reconsidering any
                    # new same-time mandatory context.
                    retry: set[str] = set()
                    for rid in sorted(ready):
                        robot = state.robots[rid]
                        if robot.causal_is_busy:
                            continue
                        result = robot.causal_control_step(now_s, state.planner)
                        reasons[result.reason] += 1
                        if schedule_control_result(rid, robot, result, now_s):
                            retry.add(rid)
                        elif result.time_cost_s <= 0.0:
                            retry.add(rid)
                    ready = retry
                    state.bus.pump(now_s)
                    ready_queued_robots(ready)
                    if state.world.all_targets_completed():
                        state.done = True
                        state.clock_s = state.mission_elapsed_time_s
                        break

                progress = self._progress_signature(state)
                if progress == last_progress:
                    stagnant_events += max(1, len(batch))
                else:
                    last_progress = progress
                    stagnant_events = 0
                if stagnant_events >= stagnation_horizon:
                    state.algorithmic_failure_type = "stagnation_horizon"
                    state.algorithmic_failure_detail = self._diagnostics(
                        state, reasons
                    )
                    break
                total_causal_events = (
                    state.events_processed + state.scheduler_events_processed
                )
                if total_causal_events >= event_horizon:
                    state.algorithmic_failure_type = "event_horizon"
                    state.algorithmic_failure_detail = self._diagnostics(
                        state, reasons
                    )
                    break
        except BaseException as exc:
            primary_failure = exc
            raise
        finally:
            if began:
                try:
                    provider.end_mission()
                except BaseException as cleanup_error:
                    if primary_failure is None:
                        raise
                    # Preserve the scientific/transport failure that caused
                    # the unwind. The cleanup problem remains available to the
                    # orchestrator as structured retained evidence.
                    setattr(
                        primary_failure,
                        "cleanup_failure",
                        f"{type(cleanup_error).__name__}: {cleanup_error}",
                    )
            state.host_program_runtime_s = max(0.0, perf_counter() - host_started)
        if not state.done and state.algorithmic_failure_type is None:
            state.algorithmic_failure_type = "event_queue_exhausted"
            state.algorithmic_failure_detail = self._diagnostics(state, reasons)
        state.validate_online_invariants()
        return state

    def _run_online_trial_legacy(
        self,
        scenario: TrialScenario,
        release_times: ReleaseTimes,
        policy: Union[ReallocationPolicy, str, Mapping[str, Any]],
        on_step: Optional[Callable[[TrialState, RobotShell, StepResult], None]] = None,
    ) -> TrialState:
        """Replay one predetermined absolute-time online arrival condition."""

        host_started = perf_counter()
        state = self.new_online_trial(scenario, release_times, policy)
        scheduler = state.reallocation_scheduler
        if scheduler is None:  # pragma: no cover - construction invariant
            raise AssertionError("online trial requires a reallocation scheduler")

        def service_global_epochs() -> None:
            for rid in sorted(state.robots):
                state.robots[rid].service_queued_allocation_epochs(state.clock_s)

        service_global_epochs()

        queue: List[OnlineEvent] = []
        order = 0
        for wake in self.initial_queue(state):
            heapq.heappush(
                queue,
                OnlineEvent(wake.time_s, 2, order, "wake", wake.rid),
            )
            order += 1
        release_times_unique = sorted({
            record.release_time_s
            for record in state.world.target_records.values()
            if record.release_time_s > 0.0
        })
        for release_s in release_times_unique:
            heapq.heappush(queue, OnlineEvent(release_s, 0, order, "release"))
            order += 1
        scheduled_timeouts: set[float] = set()

        def schedule_timeout() -> None:
            nonlocal order
            deadline = scheduler.next_timeout_s()
            if deadline is None:
                return
            key = round(float(deadline), 12)
            if key in scheduled_timeouts:
                return
            scheduled_timeouts.add(key)
            heapq.heappush(queue, OnlineEvent(float(deadline), 1, order, "timeout"))
            order += 1

        last_progress = self._progress_signature(state)
        stagnant_events = 0
        reasons: Counter[str] = Counter()
        online_event_cap = max(
            self.cfg.debug_max_events,
            1_000 + 5_000 * len(state.world.target_records),
        )
        online_stagnant_cap = max(
            self.cfg.debug_max_stagnant_events,
            500 + 100 * len(state.world.target_records),
        )
        try:
            while queue and not state.done:
                event = heapq.heappop(queue)
                state.clock_s = event.time_s
                if event.kind == "release":
                    scheduler.release_due(state.clock_s)
                    service_global_epochs()
                    state.scheduler_events_processed += 1
                    schedule_timeout()
                    last_progress = self._progress_signature(state)
                    stagnant_events = 0
                    continue
                if event.kind == "timeout":
                    scheduler.timeout_due(state.clock_s)
                    service_global_epochs()
                    state.scheduler_events_processed += 1
                    schedule_timeout()
                    last_progress = self._progress_signature(state)
                    stagnant_events = 0
                    continue
                if event.rid is None:  # pragma: no cover - queue invariant
                    raise AssertionError("wake event lacks a robot ID")
                state.bus.pump(state.clock_s)
                robot = state.robots[event.rid]
                result = robot.step(state.clock_s, state.planner)
                state.events_processed += 1
                reasons[result.reason] += 1
                if on_step:
                    on_step(state, robot, result)
                # A completion/idle/invalid-goal event may have piggybacked
                # pending admissions and opened a global epoch from inside the
                # robot step. Complete its allocation phase before termination
                # or further physical execution.
                service_global_epochs()
                if state.world.all_targets_completed():
                    state.done = True
                    break
                progress = self._progress_signature(state)
                if progress == last_progress:
                    stagnant_events += 1
                else:
                    last_progress = progress
                    stagnant_events = 0
                if stagnant_events >= online_stagnant_cap:
                    raise RuntimeError(
                        f"Stagnation detected in online trial {scenario.trial_id}; "
                        f"diagnostics={json.dumps(self._diagnostics(state, reasons), sort_keys=True, default=str)}"
                    )
                if state.events_processed >= online_event_cap:
                    raise RuntimeError(
                        f"Debug safety cap reached in online trial {scenario.trial_id}; "
                        f"diagnostics={json.dumps(self._diagnostics(state, reasons), sort_keys=True, default=str)}"
                    )
                next_wake_s = state.clock_s + self._interval_for(result, state.event_rng)
                # Sparse traces are fast-forwarded safely. Release events remain
                # in the queue and therefore retain their exact absolute times.
                if (
                    result.reason in {"no_goal", "idle"}
                    and not any(item.active_tasks for item in state.robots.values())
                    and scheduler.pending_count == 0
                    and scheduler.unreleased_count > 0
                ):
                    next_release_s = min(
                        record.release_time_s
                        for record in state.world.target_records.values()
                        if record.state == TaskState.UNRELEASED
                    )
                    next_wake_s = max(next_wake_s, next_release_s)
                heapq.heappush(
                    queue, OnlineEvent(next_wake_s, 2, order, "wake", event.rid)
                )
                order += 1
        finally:
            state.host_program_runtime_s = max(0.0, perf_counter() - host_started)
        state.validate_online_invariants()
        return state

    @staticmethod
    def _progress_signature(state: TrialState) -> tuple:
        completed = sum(record.completed for record in state.world.target_records.values())
        lifecycle = tuple(
            (record.task_id, record.state.value)
            for record in sorted(state.world.target_records.values(), key=lambda item: item.index)
        )
        positions = tuple((rid, robot.pos) for rid, robot in sorted(state.robots.items()))
        return completed, lifecycle, positions

    @staticmethod
    def _diagnostics(state: TrialState, reasons: Counter[str]) -> dict:
        return {
            "clock_s": state.clock_s,
            "events_processed": state.events_processed,
            "completed_targets": sum(record.completed for record in state.world.target_records.values()),
            "total_targets": len(state.world.target_records),
            "task_states": dict(Counter(
                record.state.value for record in state.world.target_records.values()
            )),
            "pending_tasks": (
                state.reallocation_scheduler.pending_count
                if state.reallocation_scheduler is not None else 0
            ),
            "event_reasons": dict(reasons),
            "robots": {
                rid: {
                    "pos": robot.pos,
                    "goal": robot.current_goal,
                    "active_tasks": len(robot.active_tasks),
                    "last_event": robot.last_event,
                    "no_goal_since": robot._no_goal_since,
                    "stall_recovery_count": robot._stall_recovery_count,
                    "temporary_invalid_until": {
                        str(cell): expires
                        for cell, expires in robot._temporary_invalid_task_until.items()
                    },
                    "decision": robot.last_decision_debug,
                }
                for rid, robot in sorted(state.robots.items())
            },
        }

    def _interval_for(
        self, result: StepResult, rng: Optional[random.Random] = None
    ) -> float:
        if result.reason == "turn":
            return max(self.cfg.turn_quarter_s, 1e-3)
        if result.reason == "intent_sync":
            return max(self.cfg.collision_intent_settle_s, 1e-3)
        if result.reason == "path_failed":
            return max(self.cfg.replan_delay_s, 1e-3)
        if result.reason in {"no_goal", "idle"}:
            return max(self.cfg.no_goal_delay_s, 1e-3)
        if result.moved:
            source = rng or self.rng
            jitter = source.uniform(-self.cfg.async_step_jitter_s, self.cfg.async_step_jitter_s)
            return max(1e-3, min(self.cfg.async_max_delay_s, max(self.cfg.async_min_delay_s,
                                                                  self.cfg.async_step_mean_s + jitter)))
        return max(result.time_cost_s, 1e-3)
