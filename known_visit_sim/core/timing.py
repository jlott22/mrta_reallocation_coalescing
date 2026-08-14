"""Causal allocator-timing boundary for online missions.

The simulator owns decisions and virtual event ordering.  A timing provider
owns only the duration measurement for an already-frozen group of calls.  The
objects in this module intentionally mirror the additive
``allocator_replay.causal`` boundary without importing it, so the simulator
continues to run on development machines where the hardware package is not on
``sys.path``.
"""

from __future__ import annotations

import copy
import array
import base64
import hashlib
import json
import math
import random
import sys
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, runtime_checkable

from .types import Cell


class CausalTimingError(RuntimeError):
    """A timing group was incomplete, invalid, or failed parity."""


DEVICE_ALLOCATOR_TIMER_SCOPE = (
    "allocator input integration, consensus message handling, allocator-local "
    "recovery, and choose_goal; excludes transport, message decoding, generic "
    "PSETUP synchronization, outbound extraction, serialization, and explicit "
    "pre-call GC; GC triggered naturally inside the allocator transaction "
    "remains included"
)


# Optional allocator-replay imports are deliberately lazy because ordinary
# simulator installs do not put ``Simulation/Architecture`` on ``sys.path``.
# Cache both hits and misses for the current path: retrying a failed import at
# every recursive value in a large bundle snapshot dominated experiment wall
# time even though that work is outside the allocator timer.
_OPTIONAL_CODEC_PATH: tuple[str, ...] | None = None
_OPTIONAL_CODEC_ENCODER: Any = None
_OPTIONAL_FINGERPRINT_PATH: tuple[str, ...] | None = None
_OPTIONAL_LOGICAL_SHA256: Any = None


def _optional_codec_encoder() -> Any:
    global _OPTIONAL_CODEC_PATH, _OPTIONAL_CODEC_ENCODER
    path = tuple(sys.path)
    if path != _OPTIONAL_CODEC_PATH:
        try:
            from allocator_replay.capture.codec import encode_value
        except ImportError:
            encode_value = None
        _OPTIONAL_CODEC_PATH = path
        _OPTIONAL_CODEC_ENCODER = encode_value
    return _OPTIONAL_CODEC_ENCODER


def _optional_logical_sha256() -> Any:
    global _OPTIONAL_FINGERPRINT_PATH, _OPTIONAL_LOGICAL_SHA256
    path = tuple(sys.path)
    if path != _OPTIONAL_FINGERPRINT_PATH:
        try:
            from allocator_replay.device.common.replay_fingerprint import logical_sha256
        except ImportError:
            logical_sha256 = None
        _OPTIONAL_FINGERPRINT_PATH = path
        _OPTIONAL_LOGICAL_SHA256 = logical_sha256
    return _OPTIONAL_LOGICAL_SHA256


def _canonical(value: Any) -> Any:
    """Convert simulator values to deterministic JSON-safe logical values."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return {"float": repr(value)}
        return float(value)
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: repr(pair[0]))
        }
    if isinstance(value, (set, frozenset)):
        items = [_canonical(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            "class": value.__class__.__name__,
            "attrs": _canonical(vars(value)),
        }
    return repr(value)


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        _canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_message_sha256(messages: Sequence[Mapping[str, Any]]) -> str:
    return canonical_sha256(list(messages))


def replay_encode_value(value: Any) -> Any:
    """Encode a fixture value using the hardware replay transport format.

    On an AGX runner the authoritative implementation is imported lazily from
    ``allocator_replay``.  The local implementation covers the same state
    types so ordinary simulator tests remain independent of PYTHONPATH.
    """

    encode_value = _optional_codec_encoder()
    if encode_value is not None:
        return encode_value(value)
    return _local_replay_encode_value(value)


def _local_replay_encode_value(value: Any) -> Any:
    """Recursive fallback without repeating optional-import discovery."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"@": "float", "v": "nan"}
        if math.isinf(value):
            return {"@": "float", "v": "inf" if value > 0 else "-inf"}
        return value
    if isinstance(value, bytes):
        return {"@": "bytes", "v": base64.b64encode(value).decode("ascii")}
    if isinstance(value, bytearray):
        return {"@": "bytearray", "v": base64.b64encode(value).decode("ascii")}
    if isinstance(value, array.array):
        return {
            "@": "array", "typecode": value.typecode,
            "v": [_local_replay_encode_value(item) for item in value],
        }
    if isinstance(value, random.Random):
        return {"@": "rng", "v": _local_replay_encode_value(value.getstate())}
    if isinstance(value, tuple):
        return {
            "@": "tuple",
            "v": [_local_replay_encode_value(item) for item in value],
        }
    if isinstance(value, (set, frozenset)):
        encoded = [_local_replay_encode_value(item) for item in value]
        encoded.sort(key=lambda item: json.dumps(item, sort_keys=True, default=repr))
        return {"@": "set", "v": encoded}
    if isinstance(value, list):
        return [_local_replay_encode_value(item) for item in value]
    if isinstance(value, Mapping):
        pairs = [
            [_local_replay_encode_value(key), _local_replay_encode_value(item)]
            for key, item in value.items()
        ]
        pairs.sort(key=lambda pair: json.dumps(pair[0], sort_keys=True, default=repr))
        return {"@": "dict", "v": pairs}
    if hasattr(value, "__dict__"):
        return {
            "@": "object",
            "module": value.__class__.__module__,
            "class": value.__class__.__name__,
            "attrs": _local_replay_encode_value(vars(value)),
        }
    raise TypeError(f"unsupported replay value: {type(value).__name__}")


def parity_sha256(value: Any) -> str:
    """Use the exact cross-CPython/MicroPython hash when available."""

    logical_sha256 = _optional_logical_sha256()
    if logical_sha256 is None:
        return canonical_sha256(value)
    return str(logical_sha256(replay_encode_value(value)))


def parity_encoded_sha256(encoded_value: Any) -> str:
    """Hash a value that is already in replay transport representation."""

    logical_sha256 = _optional_logical_sha256()
    if logical_sha256 is None:
        return canonical_sha256(encoded_value)
    return str(logical_sha256(encoded_value))


@dataclass(frozen=True)
class DecisionSignature:
    """Parity-relevant signature of the AGX-authoritative result."""

    goal: Optional[Cell]
    active_candidate_count: int
    message_sha256: str
    post_state_sha256: str
    call_class: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": list(self.goal) if self.goal is not None else None,
            "active_candidate_count": int(self.active_candidate_count),
            "message_sha256": self.message_sha256,
            "post_state_sha256": self.post_state_sha256,
            "call_class": self.call_class,
        }


@dataclass(frozen=True)
class MissionTimingBinding:
    """Mission identity passed once to a persistent timing provider."""

    trial_id: str
    condition_id: str
    algorithm: str
    seed: int
    robot_ids: tuple[str, ...]
    trial_config: Mapping[str, Any]
    initial_context_states: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class FrozenAllocatorCall:
    """An immutable call whose authoritative decision has already executed."""

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
    active_task_count: Optional[int] = None
    agx_choose_goal_us: int = 0
    agx_algorithm_epoch_reset_us: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def robot_id(self) -> str:
        return self.logical_robot_id

    @property
    def agx_duration_s(self) -> float:
        return int(self.agx_allocator_time_us) / 1_000_000.0

    @property
    def epoch_id(self) -> Optional[int]:
        value = self.metadata.get("epoch_id")
        return None if value is None else int(value)

    @property
    def trigger_reason(self) -> str:
        return str(self.metadata.get("trigger_reason", self.trigger_id or "other"))

    @property
    def pre_state(self) -> Mapping[str, Any]:
        value = self.metadata.get("pre_state", {})
        return value if isinstance(value, Mapping) else {}

    @property
    def pre_state_hash(self) -> str:
        return str(self.metadata.get("pre_state_sha256", ""))

    def detached_copy(self) -> "FrozenAllocatorCall":
        return FrozenAllocatorCall(
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
            agx_choose_goal_us=int(self.agx_choose_goal_us),
            agx_algorithm_epoch_reset_us=int(
                self.agx_algorithm_epoch_reset_us
            ),
            metadata=copy.deepcopy(dict(self.metadata)),
        )


@dataclass(frozen=True)
class MeasuredAllocatorCall:
    """A validated duration safe to inject into the virtual event queue."""

    call_id: str
    group_id: str
    logical_robot_id: str
    virtual_start_s: float
    virtual_completion_s: float
    device_allocator_time_us: int
    agx_allocator_time_us: int
    device_choose_goal_us: int = 0
    algorithm_epoch_reset_us: int = 0
    serial_roundtrip_us: int = 0
    host_serialization_setup_us: int = 0
    host_total_call_us: int = 0
    psetup_transaction_us: int = 0
    device_pre_call_setup_us: Optional[int] = None
    ptime_result_transaction_us: int = 0
    host_prepare_cpu_us: int = 0
    timing_decomposition_schema: int = 0
    host_serialization_setup_measured: bool = False
    device_allocator_timer_scope: str = ""
    serial_roundtrip_definition: str = ""
    parity_passed: bool = True
    timing_source: str = "simulated"
    board_id: str = ""
    serial_device: str = ""
    context_id: str = ""
    attempt_id: str = ""
    device_goal: Optional[Cell] = None
    device_message_sha256: str = ""
    device_post_state_sha256: str = ""
    physical_measurement_index: int = 0
    hardware_validated: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def device_duration_s(self) -> float:
        return int(self.device_allocator_time_us) / 1_000_000.0

    @property
    def device_choose_goal_s(self) -> float:
        return int(self.device_choose_goal_us) / 1_000_000.0

    @property
    def algorithm_epoch_reset_s(self) -> float:
        return int(self.algorithm_epoch_reset_us) / 1_000_000.0

    @property
    def serial_roundtrip_s(self) -> float:
        return int(self.serial_roundtrip_us) / 1_000_000.0

    @property
    def setup_s(self) -> float:
        return int(self.host_serialization_setup_us) / 1_000_000.0

    @property
    def device_pre_call_setup_s(self) -> Optional[float]:
        return (
            None
            if self.device_pre_call_setup_us is None
            else int(self.device_pre_call_setup_us) / 1_000_000.0
        )

    @property
    def parity_ok(self) -> bool:
        return bool(self.parity_passed)


@runtime_checkable
class CausalTimingProvider(Protocol):
    """Structural protocol implemented by mock, zero, and RP2040 providers."""

    def begin_mission(self, binding: MissionTimingBinding) -> None: ...

    def measure_group(
        self, calls: Sequence[FrozenAllocatorCall]
    ) -> Sequence[MeasuredAllocatorCall]: ...

    def end_mission(self) -> None: ...


class HostMeasuredTimingProvider:
    """Development fallback: inject the measured authoritative host duration.

    This provider is deliberately labelled as a host proxy.  Its output must
    never be represented as RP2040 hardware validation.
    """

    source = "agx_authoritative_host_proxy"

    def begin_mission(self, binding: MissionTimingBinding) -> None:
        self.binding = binding

    def measure_group(
        self, calls: Sequence[FrozenAllocatorCall]
    ) -> Sequence[MeasuredAllocatorCall]:
        return tuple(
            MeasuredAllocatorCall(
                call_id=call.call_id,
                group_id=call.group_id,
                logical_robot_id=call.logical_robot_id,
                virtual_start_s=call.virtual_start_s,
                virtual_completion_s=(
                    call.virtual_start_s + call.agx_allocator_time_us / 1_000_000.0
                ),
                device_allocator_time_us=call.agx_allocator_time_us,
                agx_allocator_time_us=call.agx_allocator_time_us,
                device_choose_goal_us=call.agx_choose_goal_us,
                algorithm_epoch_reset_us=call.agx_algorithm_epoch_reset_us,
                parity_passed=True,
                timing_source=self.source,
                context_id=call.logical_robot_id,
                device_goal=call.authoritative.goal,
                device_message_sha256=call.authoritative.message_sha256,
                device_post_state_sha256=call.authoritative.post_state_sha256,
            )
            for call in calls
        )

    def end_mission(self) -> None:
        return None


class ZeroComputeTimingProvider(HostMeasuredTimingProvider):
    """Causal counterfactual provider with exactly zero compute duration."""

    source = "zero_compute_counterfactual"

    def measure_group(
        self, calls: Sequence[FrozenAllocatorCall]
    ) -> Sequence[MeasuredAllocatorCall]:
        return tuple(
            MeasuredAllocatorCall(
                call_id=call.call_id,
                group_id=call.group_id,
                logical_robot_id=call.logical_robot_id,
                virtual_start_s=call.virtual_start_s,
                virtual_completion_s=call.virtual_start_s,
                device_allocator_time_us=0,
                agx_allocator_time_us=call.agx_allocator_time_us,
                device_choose_goal_us=0,
                algorithm_epoch_reset_us=0,
                parity_passed=True,
                timing_source=self.source,
                context_id=call.logical_robot_id,
                device_goal=call.authoritative.goal,
                device_message_sha256=call.authoritative.message_sha256,
                device_post_state_sha256=call.authoritative.post_state_sha256,
            )
            for call in calls
        )


class DeterministicTimingProvider(HostMeasuredTimingProvider):
    """Known-answer provider for causal and concurrency tests.

    ``durations`` may be a per-robot mapping, a call-ID mapping, a finite
    sequence consumed in call order, or a callable taking a frozen call.
    Values are seconds.
    """

    source = "deterministic_virtual_device"

    def __init__(
        self,
        durations: Mapping[str, float] | Sequence[float] | Callable[[FrozenAllocatorCall], float],
    ) -> None:
        self.durations = durations
        self._index = 0
        self.groups: list[tuple[FrozenAllocatorCall, ...]] = []

    def begin_mission(self, binding: MissionTimingBinding) -> None:
        super().begin_mission(binding)
        self._index = 0
        self.groups.clear()

    def _duration(self, call: FrozenAllocatorCall) -> float:
        source = self.durations
        if callable(source):
            value = source(call)
        elif isinstance(source, Mapping):
            if call.call_id in source:
                value = source[call.call_id]
            else:
                value = source[call.logical_robot_id]
        else:
            if self._index >= len(source):
                raise CausalTimingError("deterministic duration sequence exhausted")
            value = source[self._index]
            self._index += 1
        duration = float(value)
        if not math.isfinite(duration) or duration < 0.0:
            raise CausalTimingError("causal duration must be finite and non-negative")
        return duration

    def measure_group(
        self, calls: Sequence[FrozenAllocatorCall]
    ) -> Sequence[MeasuredAllocatorCall]:
        detached = tuple(call.detached_copy() for call in calls)
        self.groups.append(detached)
        rows = []
        for call in detached:
            duration_s = self._duration(call)
            duration_us = int(round(duration_s * 1_000_000.0))
            rows.append(MeasuredAllocatorCall(
                call_id=call.call_id,
                group_id=call.group_id,
                logical_robot_id=call.logical_robot_id,
                virtual_start_s=call.virtual_start_s,
                virtual_completion_s=call.virtual_start_s + duration_us / 1_000_000.0,
                device_allocator_time_us=duration_us,
                agx_allocator_time_us=call.agx_allocator_time_us,
                device_choose_goal_us=duration_us,
                algorithm_epoch_reset_us=0,
                parity_passed=True,
                timing_source=self.source,
                context_id=call.logical_robot_id,
                device_goal=call.authoritative.goal,
                device_message_sha256=call.authoritative.message_sha256,
                device_post_state_sha256=call.authoritative.post_state_sha256,
            ))
        return tuple(rows)

    def end_mission(self) -> None:
        return None


def _is_lower_sha256(value: Any) -> bool:
    text = str(value)
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


def _valid_projection_attestation(evidence: Mapping[str, Any]) -> bool:
    message_left = evidence.get("agx_message_projection_sha256")
    message_right = evidence.get("device_message_projection_sha256")
    state_left = evidence.get("agx_state_projection_sha256")
    state_right = evidence.get("device_state_projection_sha256")
    return bool(
        all(
            _is_lower_sha256(value)
            for value in (message_left, message_right, state_left, state_right)
        )
        and message_left == message_right
        and state_left == state_right
    )


def _valid_hardware_attestation(
    metadata: Any,
    board_id: str,
    measured: Any,
    frozen: FrozenAllocatorCall,
) -> bool:
    """Validate explicit per-measurement native-board evidence.

    This is intentionally fail closed.  In particular, neither a nonempty
    board label nor a hardware-looking timing source is evidence that a
    duration came from an attested RP2040.
    """

    if not isinstance(metadata, Mapping) or metadata.get("hardware_valid") is not True:
        return False
    evidence = metadata.get("hardware_attestation")
    evidence_hash = str(metadata.get("hardware_attestation_sha256", ""))
    if not isinstance(evidence, Mapping) or not evidence_hash:
        return False

    def field(*names: str) -> Any:
        for name in names:
            if name in evidence:
                return evidence[name]
        return None

    device_uid = str(field("device_id") or "").strip()
    build_id = str(field("build_id") or "").strip()
    firmware = str(field("firmware_sha256") or "")
    modules = str(field("module_set_sha256") or "")
    implementation = str(field("implementation") or "").lower()
    parity_level = str(field("parity_level") or "")
    representation_hashes_differ = field("representation_hashes_differ")
    serial_device = str(getattr(measured, "serial_device", ""))
    attempt_id = str(getattr(measured, "attempt_id", ""))
    device_signature = getattr(measured, "device", None)
    device_message_sha256 = str(
        getattr(device_signature, "message_sha256", "")
    )
    device_post_state_sha256 = str(
        getattr(device_signature, "post_state_sha256", "")
    )

    try:
        schema = int(field("schema") or 0)
        timer_resolution_us = int(field("timer_resolution_us"))
        frequency_hz = int(field("frequency_hz"))
        physical_index = int(
            getattr(measured, "physical_measurement_index", 0) or 0
        )
        attested_physical_index = int(field("physical_measurement_index"))
        evidence_hash_matches = parity_sha256(dict(evidence)) == evidence_hash
        timing_schema = int(field("timing_decomposition_schema"))
        attested_device_duration = int(field("device_allocator_time_us"))
        attested_choose_goal = int(field("device_choose_goal_us"))
        attested_epoch_reset = int(field("algorithm_epoch_reset_us"))
        attested_serial = int(field("serial_roundtrip_us"))
        attested_host_serialization = int(
            field("host_serialization_setup_us")
        )
        attested_psetup = int(field("psetup_transaction_us"))
        raw_attested_device_setup = field("device_pre_call_setup_us")
        attested_device_setup = (
            None
            if raw_attested_device_setup is None
            else int(raw_attested_device_setup)
        )
        attested_ptime = int(field("ptime_result_transaction_us"))
        attested_host_prepare = int(field("host_prepare_cpu_us"))
        attested_host_total = int(field("host_total_call_us"))
    except (TypeError, ValueError):
        return False
    measured_device_setup = getattr(measured, "device_pre_call_setup_us", None)
    measured_device_setup = (
        None if measured_device_setup is None else int(measured_device_setup)
    )
    try:
        measured_choose_goal = int(getattr(measured, "device_choose_goal_us"))
        measured_epoch_reset = int(
            getattr(measured, "algorithm_epoch_reset_us")
        )
    except (AttributeError, TypeError, ValueError):
        return False
    return bool(
        metadata.get("validation_mode") == "rp2040_native_hardware"
        and schema == 1
        and field("attestation_kind") == "rp2040_native_allocator_measurement"
        and board_id
        and device_uid == board_id
        and build_id
        and _is_lower_sha256(firmware)
        and _is_lower_sha256(modules)
        and implementation.startswith("micropython-")
        and "loopback" not in implementation
        and serial_device
        and not serial_device.upper().startswith(("LOOPBACK:", "VIRTUAL:"))
        and frequency_hz > 0
        and str(field("timer_unit") or "").lower() == "us"
        and timer_resolution_us > 0
        and field("timer_monotonic") is True
        and field("timer_wraparound_safe") is True
        and parity_level in {
            "strict_full_logical_hash",
            "shared_logical_state_and_message_effect",
        }
        and isinstance(representation_hashes_differ, bool)
        and (
            representation_hashes_differ
            == (parity_level == "shared_logical_state_and_message_effect")
        )
        and str(field("agx_message_sha256") or "")
        == frozen.authoritative.message_sha256
        and str(field("device_message_sha256") or "")
        == device_message_sha256
        and str(field("agx_post_state_sha256") or "")
        == frozen.authoritative.post_state_sha256
        and str(field("device_post_state_sha256") or "")
        == device_post_state_sha256
        and (
            parity_level != "shared_logical_state_and_message_effect"
            or _valid_projection_attestation(evidence)
        )
        and str(field("trial_id") or "") == frozen.trial_id
        and str(field("group_id") or "") == frozen.group_id
        and str(field("call_id") or "") == frozen.call_id
        and str(field("logical_context_id") or "") == frozen.logical_robot_id
        and attempt_id
        and str(field("attempt_id") or "") == attempt_id
        and physical_index > 0
        and attested_physical_index == physical_index
        and timing_schema == 2
        and attested_device_duration
        == int(getattr(measured, "device_allocator_time_us", -1))
        and attested_choose_goal == measured_choose_goal
        and attested_epoch_reset == measured_epoch_reset
        and attested_device_duration
        == attested_choose_goal + attested_epoch_reset
        and attested_choose_goal >= 0
        and attested_epoch_reset >= 0
        and attested_serial == int(getattr(measured, "serial_roundtrip_us", -1))
        and attested_host_serialization
        == int(getattr(measured, "host_serialization_setup_us", -1))
        == 0
        and attested_psetup
        == int(getattr(measured, "psetup_transaction_us", -1))
        and attested_device_setup == measured_device_setup
        and attested_ptime
        == int(getattr(measured, "ptime_result_transaction_us", -1))
        and attested_host_prepare
        == int(getattr(measured, "host_prepare_cpu_us", -1))
        and attested_host_total == int(getattr(measured, "host_total_call_us", -1))
        and field("host_serialization_setup_measured") is False
        and str(field("device_allocator_timer_scope") or "")
        == DEVICE_ALLOCATOR_TIMER_SCOPE
        and str(metadata.get("device_allocator_timer_scope", ""))
        == DEVICE_ALLOCATOR_TIMER_SCOPE
        and str(field("serial_roundtrip_definition") or "")
        == str(metadata.get("serial_roundtrip_definition", ""))
        and attested_serial == attested_psetup + attested_ptime
        and attested_device_duration >= 0
        and attested_psetup > 0
        and attested_device_setup is not None
        and attested_device_setup >= 0
        and attested_ptime > 0
        and attested_host_prepare >= 0
        and attested_host_total > 0
        and _is_lower_sha256(evidence_hash)
        and evidence_hash_matches
    )


def coerce_measurement(
    measured: Any, frozen: FrozenAllocatorCall
) -> MeasuredAllocatorCall:
    """Normalize a structurally compatible hardware-provider result."""

    call_id = str(getattr(measured, "call_id", ""))
    if call_id != frozen.call_id:
        raise CausalTimingError(
            f"timing provider returned call {call_id!r}; expected {frozen.call_id!r}"
        )
    group_id = str(getattr(measured, "group_id", frozen.group_id))
    if group_id != frozen.group_id:
        raise CausalTimingError("timing result belongs to another same-time group")
    robot_id = str(
        getattr(
            measured,
            "logical_robot_id",
            getattr(measured, "robot_id", frozen.logical_robot_id),
        )
    )
    if robot_id != frozen.logical_robot_id:
        raise CausalTimingError("timing result belongs to another logical context")
    parity = getattr(
        measured, "parity_passed", getattr(measured, "parity_ok", False)
    )
    if parity is not True:
        raise CausalTimingError(f"allocator parity failed for {frozen.call_id}")
    raw_us = getattr(measured, "device_allocator_time_us", None)
    if raw_us is None:
        raw_s = getattr(measured, "device_duration_s", None)
        if raw_s is None:
            raise CausalTimingError("timing result has no device allocator duration")
        raw_us = round(float(raw_s) * 1_000_000.0)
    duration_us = int(raw_us)
    if duration_us < 0:
        raise CausalTimingError("device allocator duration must be non-negative")
    expected_completion = frozen.virtual_start_s + duration_us / 1_000_000.0
    completion = float(getattr(measured, "virtual_completion_s", expected_completion))
    if not math.isfinite(completion) or abs(completion - expected_completion) > 1e-9:
        raise CausalTimingError(
            "provider completion must equal shared virtual start plus its own duration"
        )
    def microseconds(name: str, fallback_s: str = "") -> int:
        value = getattr(measured, name, None)
        if value is None and fallback_s:
            value = round(float(getattr(measured, fallback_s, 0.0)) * 1_000_000.0)
        return max(0, int(value or 0))

    metadata = getattr(measured, "metadata", {})
    metadata_mapping = metadata if isinstance(metadata, Mapping) else {}

    def allocator_component_us(name: str) -> Optional[int]:
        attribute_value = getattr(measured, name, None)
        metadata_value = metadata_mapping.get(name)
        if attribute_value is not None and metadata_value is not None:
            try:
                if int(attribute_value) != int(metadata_value):
                    raise CausalTimingError(
                        f"{name} disagrees between measurement and metadata"
                    )
            except (TypeError, ValueError) as exc:
                raise CausalTimingError(
                    f"{name} must be an integer microsecond duration"
                ) from exc
        value = attribute_value if attribute_value is not None else metadata_value
        if value is None:
            return None
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise CausalTimingError(
                f"{name} must be an integer microsecond duration"
            ) from exc
        if result < 0:
            raise CausalTimingError(f"{name} must be non-negative")
        return result

    device_choose_goal_us = allocator_component_us("device_choose_goal_us")
    algorithm_epoch_reset_us = allocator_component_us(
        "algorithm_epoch_reset_us"
    )
    # Schema-zero structural providers written before the split contract are
    # still unambiguous: their whole allocator timer was choose_goal. Schema
    # two providers must explicitly report both components below.
    split_fields_present = (
        device_choose_goal_us is not None
        and algorithm_epoch_reset_us is not None
    )
    if device_choose_goal_us is None and algorithm_epoch_reset_us is None:
        device_choose_goal_us = duration_us
        algorithm_epoch_reset_us = 0
    elif not split_fields_present:
        raise CausalTimingError(
            "device allocator timing decomposition is only partially present"
        )
    if duration_us != device_choose_goal_us + algorithm_epoch_reset_us:
        raise CausalTimingError(
            "device allocator duration must equal device_choose_goal_us plus "
            "algorithm_epoch_reset_us"
        )
    psetup_transaction_us = microseconds("psetup_transaction_us")
    ptime_result_transaction_us = microseconds(
        "ptime_result_transaction_us"
    )
    host_prepare_cpu_us = microseconds("host_prepare_cpu_us")
    raw_device_setup_us = getattr(measured, "device_pre_call_setup_us", None)
    if raw_device_setup_us is None and isinstance(metadata, Mapping):
        raw_device_setup_us = metadata.get("device_pre_call_setup_us")
    try:
        device_pre_call_setup_us = (
            None
            if raw_device_setup_us is None
            else max(0, int(raw_device_setup_us))
        )
        timing_decomposition_schema = int(
            metadata_mapping.get("timing_decomposition_schema", 0)
        )
    except (TypeError, ValueError) as exc:
        raise CausalTimingError("invalid timing decomposition metadata") from exc
    host_serialization_setup_measured = bool(
        metadata_mapping.get("host_serialization_setup_measured", False)
    )
    device_allocator_timer_scope = str(
        metadata_mapping.get("device_allocator_timer_scope", "")
    )
    serial_roundtrip_definition = str(
        metadata_mapping.get("serial_roundtrip_definition", "")
    )
    serial_roundtrip_us = microseconds(
        "serial_roundtrip_us", "serial_roundtrip_s"
    )
    host_serialization_setup_us = microseconds(
        "host_serialization_setup_us", "setup_s"
    )
    if timing_decomposition_schema == 2:
        if not split_fields_present:
            raise CausalTimingError(
                "schema-2 allocator component durations are missing"
            )
        if serial_roundtrip_us != (
            psetup_transaction_us + ptime_result_transaction_us
        ):
            raise CausalTimingError("schema-2 serial timing decomposition is inconsistent")
        if host_serialization_setup_us != 0 or host_serialization_setup_measured:
            raise CausalTimingError(
                "schema-2 host serialization must be zero and explicitly unmeasured"
            )
        if device_allocator_timer_scope != DEVICE_ALLOCATOR_TIMER_SCOPE:
            raise CausalTimingError(
                "schema-2 device allocator timer scope is not canonical"
            )
        if not serial_roundtrip_definition:
            raise CausalTimingError("schema-2 timing definitions are missing")
    device_signature = getattr(measured, "device", None)
    device_goal = getattr(measured, "device_goal", None)
    if device_goal is None and device_signature is not None:
        device_goal = getattr(device_signature, "goal", None)
    device_message_hash = str(
        getattr(measured, "device_message_sha256", "") or (
            getattr(device_signature, "message_sha256", "")
            if device_signature is not None else ""
        )
    )
    device_state_hash = str(
        getattr(measured, "device_post_state_sha256", "") or (
            getattr(device_signature, "post_state_sha256", "")
            if device_signature is not None else ""
        )
    )
    normalized_device_goal = (
        None
        if device_goal is None
        else (int(device_goal[0]), int(device_goal[1]))
    )
    if normalized_device_goal != frozen.authoritative.goal:
        raise CausalTimingError(
            f"device goal disagrees with authoritative result for {frozen.call_id}"
        )
    if device_signature is not None:
        if (
            int(getattr(device_signature, "active_candidate_count", -1))
            != frozen.authoritative.active_candidate_count
        ):
            raise CausalTimingError(
                f"device candidate count disagrees for {frozen.call_id}"
            )
        if (
            str(getattr(device_signature, "call_class", ""))
            != frozen.authoritative.call_class
        ):
            raise CausalTimingError(
                f"device call classification disagrees for {frozen.call_id}"
            )
    direct_hash_match = bool(
        device_message_hash
        and device_state_hash
        and device_message_hash == frozen.authoritative.message_sha256
        and device_state_hash == frozen.authoritative.post_state_sha256
    )
    projected_hash_match = bool(
        isinstance(metadata, Mapping)
        and metadata.get("parity_level")
        == "shared_logical_state_and_message_effect"
        and _valid_projection_attestation(metadata)
    )
    if not (direct_hash_match or projected_hash_match):
        raise CausalTimingError(
            f"device message/state parity evidence disagrees for {frozen.call_id}"
        )
    board_id = str(getattr(measured, "board_id", ""))
    # A label that happens not to equal a simulator sentinel is not hardware
    # evidence.  The native session must explicitly attest the live identity,
    # build, firmware, module set, and device timer; detailed validation is
    # kept in one helper so simulated/spoof providers remain unambiguously
    # non-hardware even when they populate a convincing-looking board ID.
    hardware_validated = _valid_hardware_attestation(
        metadata, board_id, measured, frozen
    )
    return MeasuredAllocatorCall(
        call_id=frozen.call_id,
        group_id=group_id,
        logical_robot_id=robot_id,
        virtual_start_s=frozen.virtual_start_s,
        virtual_completion_s=completion,
        device_allocator_time_us=duration_us,
        agx_allocator_time_us=microseconds(
            "agx_allocator_time_us"
        ) or frozen.agx_allocator_time_us,
        device_choose_goal_us=device_choose_goal_us,
        algorithm_epoch_reset_us=algorithm_epoch_reset_us,
        serial_roundtrip_us=serial_roundtrip_us,
        host_serialization_setup_us=host_serialization_setup_us,
        host_total_call_us=microseconds("host_total_call_us"),
        psetup_transaction_us=psetup_transaction_us,
        device_pre_call_setup_us=device_pre_call_setup_us,
        ptime_result_transaction_us=ptime_result_transaction_us,
        host_prepare_cpu_us=host_prepare_cpu_us,
        timing_decomposition_schema=timing_decomposition_schema,
        host_serialization_setup_measured=host_serialization_setup_measured,
        device_allocator_timer_scope=device_allocator_timer_scope,
        serial_roundtrip_definition=serial_roundtrip_definition,
        parity_passed=True,
        timing_source=str(
            getattr(measured, "timing_source", "rp2040_device_allocator")
        ),
        board_id=board_id,
        serial_device=str(getattr(measured, "serial_device", "")),
        context_id=str(getattr(measured, "context_id", frozen.logical_robot_id)),
        attempt_id=str(getattr(measured, "attempt_id", "")),
        device_goal=normalized_device_goal,
        device_message_sha256=device_message_hash,
        device_post_state_sha256=device_state_hash,
        physical_measurement_index=max(
            0, int(getattr(measured, "physical_measurement_index", 0) or 0)
        ),
        hardware_validated=hardware_validated,
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def validate_and_index_measurements(
    calls: Sequence[FrozenAllocatorCall], measured: Sequence[Any]
) -> dict[str, MeasuredAllocatorCall]:
    """Fail closed unless one valid result exists for every frozen call."""

    expected = {call.call_id: call for call in calls}
    if len(expected) != len(calls):
        raise CausalTimingError("frozen call IDs are not unique")
    raw_by_id: dict[str, Any] = {}
    for item in measured:
        call_id = str(getattr(item, "call_id", ""))
        if call_id not in expected or call_id in raw_by_id:
            raise CausalTimingError(f"unexpected or duplicate timing result {call_id!r}")
        raw_by_id[call_id] = item
    if set(raw_by_id) != set(expected):
        missing = sorted(set(expected) - set(raw_by_id))
        raise CausalTimingError(f"timing group omitted calls: {missing}")
    return {
        call_id: coerce_measurement(raw_by_id[call_id], frozen)
        for call_id, frozen in expected.items()
    }
