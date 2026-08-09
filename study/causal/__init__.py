"""Causal RP2040-timed campaign infrastructure.

This package is deliberately versioned separately from :mod:`study.campaign`,
which preserves the historical noncausal AGX study.  The causal path binds one
long-lived worker process to one physical board and schedules paired policy
blocks, not independent condition jobs.
"""

from .model import (
    BoardBinding,
    CausalConfig,
    CausalJob,
    PairedBlock,
    PolicySpec,
    load_causal_config,
)

__all__ = [
    "BoardBinding",
    "CausalConfig",
    "CausalJob",
    "PairedBlock",
    "PolicySpec",
    "load_causal_config",
]
