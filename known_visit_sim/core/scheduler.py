from __future__ import annotations

import heapq
import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable, Dict, List, Mapping, Optional, Type, Union

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
from .types import TrialScenario
from .world import World


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

    @property
    def simulated_execution_time_s(self) -> float:
        return float(self.clock_s)

    @property
    def cumulative_allocator_time_s(self) -> float:
        return sum(
            sum(robot.counters.allocator_time_ns_samples)
            for robot in self.robots.values()
        ) / 1_000_000_000.0

    @property
    def allocator_parallel_critical_path_time_s(self) -> float:
        from .reallocation import allocator_parallel_critical_path_s

        scheduler = self.reallocation_scheduler
        calls = scheduler.allocator_calls if scheduler is not None else []
        return allocator_parallel_critical_path_s(calls)

    @property
    def mission_elapsed_time_serial_compute_s(self) -> float:
        return self.simulated_execution_time_s + self.cumulative_allocator_time_s

    @property
    def mission_elapsed_time_s(self) -> float:
        return self.simulated_execution_time_s + self.allocator_parallel_critical_path_time_s

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

    def serializable_online_result(self) -> dict:
        scheduler = self.reallocation_scheduler
        return {
            "summary": self.online_metrics(),
            "policy": scheduler.policy.to_dict() if scheduler else None,
            "tasks": self.task_rows(),
            "allocation_epochs": self.epoch_rows(),
            "allocator_calls": self.allocator_call_rows(),
            "pending_queue_samples": self.queue_sample_rows(),
        }

    def validate_online_invariants(self) -> None:
        expected = (
            self.simulated_execution_time_s
            + self.allocator_parallel_critical_path_time_s
        )
        if abs(self.mission_elapsed_time_s - expected) > 1e-9:
            raise AssertionError("mission elapsed time arithmetic is inconsistent")
        for record in self.world.target_records.values():
            timestamps = [
                record.released_time_s,
                record.admission_time_s,
                record.first_assignment_time_s,
                record.first_completion_time_s,
            ]
            observed = [value for value in timestamps if value is not None]
            if observed != sorted(observed):
                raise AssertionError(f"task timestamp order invalid for {record.task_id}")
            if record.completed and record.first_assignment_time_s is None:
                raise AssertionError(f"completed task {record.task_id} lacks an assignment")


class AsyncTrialRunner:
    def __init__(self, cfg: SimConfig, allocator_cls: Type[AllocatorBase],
                 comm_model: CommunicationModel, seed: int = 0) -> None:
        self.cfg = cfg
        self.allocator_cls = allocator_cls
        self.comm_model = comm_model
        self.seed = int(seed)
        self.rng = random.Random(seed)

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
