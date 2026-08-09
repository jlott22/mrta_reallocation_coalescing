"""Unrestricted Collaborative Visit HIL for reallocation coalescing.

This is the only campaign package used by ``python -m allocator_replay``.
The copied Top-K/Bayesian modules are retained as read-only implementation
history, but are deliberately not imported by this package.
"""

from .config import (
    ALLOCATORS,
    CampaignConfig,
    HilCondition,
    PolicySpec,
    load_campaign_config,
)

__all__ = (
    "ALLOCATORS",
    "CampaignConfig",
    "HilCondition",
    "PolicySpec",
    "load_campaign_config",
)
