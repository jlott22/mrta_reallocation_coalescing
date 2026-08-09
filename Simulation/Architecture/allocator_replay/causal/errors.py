"""Fail-closed errors for causal RP2040 timing sessions."""

from __future__ import annotations

from typing import Any


class CausalTimingError(RuntimeError):
    """Base class for failures that invalidate a hardware-timed attempt."""


class BoardBindingError(CausalTimingError):
    """A live serial endpoint cannot be bound to the intended physical board."""


class BoardLeaseError(CausalTimingError):
    """A physical board is already owned by another hardware worker."""


class SessionStateError(CausalTimingError):
    """A call violates the deterministic mission/context lifecycle."""


class StaleReplyError(CausalTimingError):
    """A reply belongs to another call, group, trial, or device session."""


class DeviceCallError(CausalTimingError):
    """The timing device failed before a valid duration could be accepted."""


class ParityFailure(CausalTimingError):
    """RP2040 output differs from the staged AGX-authoritative output."""

    def __init__(self, message: str, diagnostics: dict[str, Any]) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics
