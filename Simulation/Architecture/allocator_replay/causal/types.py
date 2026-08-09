"""Typed boundary between a causal simulator and one RP2040 timing worker.

The simulator freezes every member of a same-virtual-time group and runs its
AGX-authoritative allocator calls before invoking the timing provider.  The
provider samples the physical board serially, but every completion timestamp
is derived from the common virtual start rather than physical request order.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from allocator_replay.capture.codec import decode_value, encode_value
from allocator_replay.device.common.replay_fingerprint import logical_sha256


PRIMARY_ALGORITHMS = ("CBAA", "ACBBA", "PI", "HIPC")
OPTIONAL_ALGORITHMS = ("DMCHBA", "DGA")
SUPPORTED_ALGORITHMS = PRIMARY_ALGORITHMS + OPTIONAL_ALGORITHMS
LOGICAL_CONTEXT_COUNT = 4


def semantic_hash(value: Any) -> str:
    """Return the replay runtime's cross-CPython/MicroPython logical hash."""

    return str(logical_sha256(encode_value(value)))


def _normal_goal(value: Any) -> tuple[int, int] | None:
    value = decode_value(value)
    if value is None:
        return None
    if isinstance(value, Mapping) and "goal" in value:
        value = value["goal"]
    try:
        if len(value) != 2:  # type: ignore[arg-type]
            raise ValueError
        return int(value[0]), int(value[1])  # type: ignore[index]
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"invalid allocator goal: {value!r}") from exc


@dataclass(frozen=True)
class DecisionSignature:
    """Parity-relevant result of one allocator call.

    ``post_state`` and messages may contain arrays, sets, encoded replay
    values, or ordinary Python containers.  Hashes use the same normalized
    logical fingerprint implementation deployed with the device bundle.
    """

    goal: tuple[int, int] | None
    active_candidate_count: int
    message_sha256: str
    post_state_sha256: str
    call_class: str

    @classmethod
    def from_result(
        cls,
        result: Mapping[str, Any],
        *,
        active_candidate_count: int | None = None,
    ) -> "DecisionSignature":
        messages = decode_value(result.get("messages", []))
        post_state = decode_value(
            result.get("post_state", result.get("state", {}))
        )
        if active_candidate_count is None:
            raw_count = result.get(
                "candidate_count_after",
                result.get("active_candidate_count", result.get("candidate_count")),
            )
            if raw_count is None:
                raise ValueError("allocator result has no active candidate count")
            active_candidate_count = int(raw_count)
        if active_candidate_count < 0:
            raise ValueError("active candidate count must be non-negative")
        call_class = str(result.get("call_class", "")).strip()
        if not call_class:
            raise ValueError("allocator result has no call classification")
        return cls(
            goal=_normal_goal(result.get("goal")),
            active_candidate_count=int(active_candidate_count),
            message_sha256=semantic_hash(messages),
            post_state_sha256=semantic_hash(post_state),
            call_class=call_class,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": None if self.goal is None else list(self.goal),
            "active_candidate_count": self.active_candidate_count,
            "message_sha256": self.message_sha256,
            "post_state_sha256": self.post_state_sha256,
            "call_class": self.call_class,
        }


@dataclass(frozen=True)
class FrozenCall:
    """One immutable logical call whose AGX decision is already staged."""

    call_id: str
    group_id: str
    trial_id: str
    logical_robot_id: str
    algorithm: str
    virtual_start_s: float
    device_setup: Mapping[str, Any]
    authoritative: DecisionSignature
    agx_allocator_time_us: int
    trigger_id: str = ""
    active_task_count: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for label, value in (
            ("call_id", self.call_id),
            ("group_id", self.group_id),
            ("trial_id", self.trial_id),
            ("logical_robot_id", self.logical_robot_id),
        ):
            if not str(value):
                raise ValueError(f"{label} must not be empty")
        algorithm = str(self.algorithm).upper()
        if algorithm not in SUPPORTED_ALGORITHMS:
            raise ValueError(f"unsupported allocator: {algorithm}")
        if float(self.virtual_start_s) < 0:
            raise ValueError("virtual_start_s must be non-negative")
        if int(self.agx_allocator_time_us) < 0:
            raise ValueError("AGX allocator duration must be non-negative")
        object.__setattr__(self, "algorithm", algorithm)

    def detached_copy(self) -> "FrozenCall":
        """Deep-copy mutable inputs before any physical call can run."""

        return FrozenCall(
            call_id=self.call_id,
            group_id=self.group_id,
            trial_id=self.trial_id,
            logical_robot_id=self.logical_robot_id,
            algorithm=self.algorithm,
            virtual_start_s=float(self.virtual_start_s),
            device_setup=copy.deepcopy(dict(self.device_setup)),
            authoritative=self.authoritative,
            agx_allocator_time_us=int(self.agx_allocator_time_us),
            trigger_id=self.trigger_id,
            active_task_count=self.active_task_count,
            metadata=copy.deepcopy(dict(self.metadata)),
        )

    @property
    def robot_id(self) -> str:
        return self.logical_robot_id

    @property
    def logical_context_id(self) -> str:
        return self.logical_robot_id


@dataclass(frozen=True)
class MissionBinding:
    """Identity and deterministic context initialization for one mission."""

    trial_id: str
    condition_id: str
    algorithm: str
    seed: int
    robot_ids: tuple[str, ...]
    trial_config: Mapping[str, Any]
    initial_context_states: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        algorithm = str(self.algorithm).upper()
        if algorithm not in SUPPORTED_ALGORITHMS:
            raise ValueError(f"unsupported allocator: {algorithm}")
        if len(self.robot_ids) != LOGICAL_CONTEXT_COUNT:
            raise ValueError("a hardware-timed mission requires exactly four contexts")
        if len(set(self.robot_ids)) != LOGICAL_CONTEXT_COUNT:
            raise ValueError("logical robot/context IDs must be unique")
        if set(self.initial_context_states) != set(self.robot_ids):
            raise ValueError("initial context states must match the four robot IDs")
        if not self.trial_id or not self.condition_id:
            raise ValueError("trial_id and condition_id are required")
        object.__setattr__(self, "algorithm", algorithm)


@dataclass(frozen=True)
class MeasuredCall:
    """A parity-valid duration sample safe to inject into virtual time."""

    call_id: str
    group_id: str
    trial_id: str
    logical_robot_id: str
    algorithm: str
    board_id: str
    serial_device: str
    context_id: str
    attempt_id: str
    virtual_start_s: float
    virtual_completion_s: float
    agx_allocator_time_us: int
    device_allocator_time_us: int
    serial_roundtrip_us: int
    host_serialization_setup_us: int
    host_total_call_us: int
    parity_passed: bool
    authoritative: DecisionSignature
    device: DecisionSignature
    device_goal: tuple[int, int] | None
    device_messages: tuple[Any, ...]
    device_post_state: Mapping[str, Any]
    candidate_count_before: int
    candidate_count_after: int
    heap_free_before: int | None
    heap_free_after: int | None
    physical_measurement_index: int
    psetup_transaction_us: int = 0
    device_pre_call_setup_us: int | None = None
    ptime_result_transaction_us: int = 0
    host_prepare_cpu_us: int = 0
    device_choose_goal_us: int = 0
    algorithm_epoch_reset_us: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def robot_id(self) -> str:
        return self.logical_robot_id

    @property
    def parity_ok(self) -> bool:
        return self.parity_passed

    @property
    def device_duration_s(self) -> float:
        return self.device_allocator_time_us / 1_000_000.0

    @property
    def agx_duration_s(self) -> float:
        return self.agx_allocator_time_us / 1_000_000.0

    @property
    def serial_roundtrip_s(self) -> float:
        return self.serial_roundtrip_us / 1_000_000.0

    @property
    def setup_s(self) -> float:
        return self.host_serialization_setup_us / 1_000_000.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "group_id": self.group_id,
            "trial_id": self.trial_id,
            "logical_robot_id": self.logical_robot_id,
            "algorithm": self.algorithm,
            "board_id": self.board_id,
            "serial_device": self.serial_device,
            "context_id": self.context_id,
            "attempt_id": self.attempt_id,
            "virtual_start_s": self.virtual_start_s,
            "virtual_completion_s": self.virtual_completion_s,
            "agx_allocator_time_us": self.agx_allocator_time_us,
            "device_allocator_time_us": self.device_allocator_time_us,
            "serial_roundtrip_us": self.serial_roundtrip_us,
            "host_serialization_setup_us": self.host_serialization_setup_us,
            "host_total_call_us": self.host_total_call_us,
            "parity_passed": self.parity_passed,
            "authoritative": self.authoritative.as_dict(),
            "device": self.device.as_dict(),
            "candidate_count_before": self.candidate_count_before,
            "candidate_count_after": self.candidate_count_after,
            "heap_free_before": self.heap_free_before,
            "heap_free_after": self.heap_free_after,
            "physical_measurement_index": self.physical_measurement_index,
            "psetup_transaction_us": self.psetup_transaction_us,
            "device_pre_call_setup_us": self.device_pre_call_setup_us,
            "ptime_result_transaction_us": (
                self.ptime_result_transaction_us
            ),
            "host_prepare_cpu_us": self.host_prepare_cpu_us,
            "device_choose_goal_us": self.device_choose_goal_us,
            "algorithm_epoch_reset_us": self.algorithm_epoch_reset_us,
            "metadata": dict(self.metadata),
        }


def validate_same_time_group(calls: Sequence[FrozenCall]) -> None:
    if not calls:
        raise ValueError("a measurement group must not be empty")
    first = calls[0]
    for item in calls[1:]:
        if item.group_id != first.group_id:
            raise ValueError("one measurement group cannot mix group IDs")
        if item.trial_id != first.trial_id:
            raise ValueError("one measurement group cannot mix trials")
        if item.virtual_start_s != first.virtual_start_s:
            raise ValueError("same-time calls must have one virtual start timestamp")
    context_ids = [item.logical_robot_id for item in calls]
    if len(context_ids) != len(set(context_ids)):
        raise ValueError("a logical context may appear only once in a same-time group")
    call_ids = [item.call_id for item in calls]
    if len(call_ids) != len(set(call_ids)):
        raise ValueError("call IDs must be unique within a group")
