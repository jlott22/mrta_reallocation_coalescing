"""Persistent native allocators for the collaborative-visit mission.

The package deliberately avoids imports from either desktop simulator.  The
same modules can therefore be frozen/compiled for MicroPython and imported by
the host-side hardware-in-the-loop adapter.
"""

from .runtime import PersistentCollaborativeRuntime, create_persistent_runtime

__all__ = ("PersistentCollaborativeRuntime", "create_persistent_runtime")
