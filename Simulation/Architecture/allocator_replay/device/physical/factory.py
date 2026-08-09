"""Complete Collaborative Visit allocator factory shared by HIL and robots.

This module is flattened to ``replay_physical_factory`` in a device build.
It deliberately selects the same deployed allocator modules for both entry
points so stationary and moving trials time the same allocator code.
"""

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
    if mission not in ("collaborative", "collaborative_visit"):
        raise ValueError(
            "this study deploys Collaborative Visit allocators only: " + mission
        )
    if config.get("max_candidate_cells") is not None:
        raise ValueError("reallocation-coalescing HIL requires unrestricted candidates")
    module = __import__("replay_native_c_runtime")
    return module.create_persistent_runtime(config)
