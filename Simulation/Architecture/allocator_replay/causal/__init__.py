"""Causal AGX-authoritative/RP2040-timed allocator integration."""

from .binding import (
    REQUIRED_HARDWARE_WORKERS,
    BoardFingerprint,
    BoardLease,
    StableBoardBinding,
    bind_hardware_workers,
    bind_single_hardware_worker,
)
from .errors import (
    BoardBindingError,
    BoardLeaseError,
    CausalTimingError,
    DeviceCallError,
    ParityFailure,
    SessionStateError,
    StaleReplyError,
)
from .preflight import (
    CausalPreflightRecorder,
    PREFLIGHT_REQUIREMENTS,
    load_and_verify_preflight,
    run_native_preflight,
    verify_preflight_report,
    write_preflight_reports,
)
from .parity import project_messages, project_state, projected_parity
from .providers import SimulatedDurationProvider, ZeroDurationProvider
from .session import (
    CausalBoardSession,
    coerce_frozen_call,
    coerce_mission_binding,
)
from .types import (
    LOGICAL_CONTEXT_COUNT,
    OPTIONAL_ALGORITHMS,
    PRIMARY_ALGORITHMS,
    SUPPORTED_ALGORITHMS,
    DecisionSignature,
    FrozenCall,
    MeasuredCall,
    MissionBinding,
    semantic_hash,
)
from .virtual_device import DeterministicVirtualDevice

__all__ = [
    "BoardBindingError",
    "BoardFingerprint",
    "BoardLease",
    "BoardLeaseError",
    "CausalBoardSession",
    "CausalPreflightRecorder",
    "CausalTimingError",
    "DecisionSignature",
    "DeterministicVirtualDevice",
    "DeviceCallError",
    "FrozenCall",
    "LOGICAL_CONTEXT_COUNT",
    "MeasuredCall",
    "MissionBinding",
    "OPTIONAL_ALGORITHMS",
    "PREFLIGHT_REQUIREMENTS",
    "PRIMARY_ALGORITHMS",
    "ParityFailure",
    "REQUIRED_HARDWARE_WORKERS",
    "SUPPORTED_ALGORITHMS",
    "SessionStateError",
    "SimulatedDurationProvider",
    "StableBoardBinding",
    "StaleReplyError",
    "ZeroDurationProvider",
    "bind_hardware_workers",
    "bind_single_hardware_worker",
    "coerce_frozen_call",
    "coerce_mission_binding",
    "load_and_verify_preflight",
    "project_messages",
    "project_state",
    "projected_parity",
    "run_native_preflight",
    "semantic_hash",
    "verify_preflight_report",
    "write_preflight_reports",
]
