"""Complete persistent allocator factory shared by HIL and physical wrappers.

This module is flattened to ``replay_physical_factory`` in a device build.
It deliberately selects the same deployed allocator modules for both entry
points so stationary and moving trials time the same allocator code.
"""

from replay_persistent import ReplayPersistentRuntime


ALGORITHM_CLASSES = {
    "CBAA": "CBAAAllocator",
    "ACBBA": "ACBBAAllocator",
    "PI": "PIAllocator",
    "HIPC": "HIPCAllocator",
    "DMCHBA": "DMCHBAAllocator",
    "DGA": "DGAAllocator",
}


def create_complete_runtime(config):
    mission = str(config.get("mission", "")).lower()
    algorithm = str(config.get("algorithm", "")).upper()
    if algorithm not in ALGORITHM_CLASSES:
        raise ValueError("unknown allocator: " + algorithm)
    if mission == "collaborative":
        module = __import__("replay_native_c_runtime")
        return module.create_persistent_runtime(config)
    if mission != "bayesian":
        raise ValueError("unknown mission: " + mission)
    if algorithm == "DMCHBA":
        module = __import__("replay_native_b_dmchba")
        persistent = __import__("replay_persistent")
        return persistent.ReplayPersistentRuntime(
            lambda: module.DMCHBAAllocator(config)
        )
    module = __import__("replay_b_" + algorithm.lower())
    allocator_class = getattr(module, ALGORITHM_CLASSES[algorithm])
    return ReplayPersistentRuntime(lambda: allocator_class())
