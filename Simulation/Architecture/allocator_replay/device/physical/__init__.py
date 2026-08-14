"""Motor-free adapter for using one persistent allocator in a physical loop."""

from .adapter import (
    DEVICE_ALLOCATOR_TIMER_SCOPE,
    PhysicalAllocatorAdapter,
)

__all__ = ("DEVICE_ALLOCATOR_TIMER_SCOPE", "PhysicalAllocatorAdapter")
