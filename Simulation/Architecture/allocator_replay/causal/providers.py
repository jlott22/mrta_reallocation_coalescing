"""Software-only timing providers sharing the native group contract."""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, Sequence

from .errors import SessionStateError
from .session import coerce_frozen_call, coerce_mission_binding
from .types import MeasuredCall, MissionBinding, validate_same_time_group


DurationFunction = Callable[[Any], int]


class SimulatedDurationProvider:
    """Deterministic duration source for causal simulator tests and pilots.

    Durations are integer microseconds.  Outputs are tagged as non-hardware so
    they can never satisfy a native campaign/preflight gate.
    """

    def __init__(self, duration_us: int | Mapping[str, int] | DurationFunction) -> None:
        self.duration_source = duration_us
        self.mission: MissionBinding | None = None
        self.seen_calls: set[str] = set()
        self.seen_groups: set[str] = set()
        self.measurement_index = 0

    def begin_mission(self, mission: Any) -> None:
        if self.mission is not None:
            raise SessionStateError("simulated duration provider already has a mission")
        self.mission = coerce_mission_binding(mission)
        self.seen_calls.clear()
        self.seen_groups.clear()
        self.measurement_index = 0

    def _duration(self, call: Any) -> int:
        source = self.duration_source
        if callable(source):
            value = source(call)
        elif isinstance(source, Mapping):
            value = source[call.call_id]
        else:
            value = source
        duration = int(value)
        if duration < 0:
            raise ValueError("simulated duration must be non-negative")
        return duration

    def measure_group(self, calls: Sequence[Any]) -> tuple[MeasuredCall, ...]:
        if self.mission is None:
            raise SessionStateError("simulated provider has no active mission")
        detached = tuple(coerce_frozen_call(item).detached_copy() for item in calls)
        validate_same_time_group(detached)
        if detached[0].trial_id != self.mission.trial_id:
            raise SessionStateError("simulated group belongs to another mission")
        if detached[0].group_id in self.seen_groups:
            raise SessionStateError("duplicate simulated group ID")
        if any(item.call_id in self.seen_calls for item in detached):
            raise SessionStateError("duplicate simulated call ID")
        self.seen_groups.add(detached[0].group_id)
        self.seen_calls.update(item.call_id for item in detached)
        measured: list[MeasuredCall] = []
        for call in detached:
            self.measurement_index += 1
            duration = self._duration(call)
            metadata = copy.deepcopy(dict(call.metadata))
            metadata.update(
                {
                    "hardware_valid": False,
                    "validation_mode": "software_simulated_duration",
                }
            )
            measured.append(
                MeasuredCall(
                    call_id=call.call_id,
                    group_id=call.group_id,
                    trial_id=call.trial_id,
                    logical_robot_id=call.logical_robot_id,
                    algorithm=call.algorithm,
                    board_id="SIMULATED_DURATION",
                    serial_device="NONE",
                    context_id=call.logical_robot_id,
                    attempt_id=f"simulated-{self.measurement_index:08d}",
                    virtual_start_s=call.virtual_start_s,
                    virtual_completion_s=call.virtual_start_s + duration / 1_000_000.0,
                    agx_allocator_time_us=call.agx_allocator_time_us,
                    device_allocator_time_us=duration,
                    device_choose_goal_us=duration,
                    algorithm_epoch_reset_us=0,
                    serial_roundtrip_us=0,
                    host_serialization_setup_us=0,
                    host_total_call_us=0,
                    parity_passed=True,
                    authoritative=call.authoritative,
                    device=call.authoritative,
                    device_goal=call.authoritative.goal,
                    device_messages=(),
                    device_post_state={},
                    candidate_count_before=call.authoritative.active_candidate_count,
                    candidate_count_after=call.authoritative.active_candidate_count,
                    heap_free_before=None,
                    heap_free_after=None,
                    physical_measurement_index=self.measurement_index,
                    metadata=metadata,
                )
            )
        return tuple(measured)

    def end_mission(self) -> None:
        self.mission = None
        self.seen_calls.clear()
        self.seen_groups.clear()

    def open(self) -> "SimulatedDurationProvider":
        return self

    def close(self) -> None:
        self.end_mission()


class ZeroDurationProvider(SimulatedDurationProvider):
    """Causal zero-compute counterfactual provider."""

    def __init__(self) -> None:
        super().__init__(0)
