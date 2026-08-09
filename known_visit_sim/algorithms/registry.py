from __future__ import annotations

import importlib
from typing import Type

from .base import AllocatorBase


def load_allocator_class(spec: str) -> Type[AllocatorBase]:
    """Load a built-in allocator name or an explicit ``module:Class``."""
    aliases = {
        "cbaa": "known_visit_sim.algorithms.CBAA:CBAAAllocator",
        "acbba": "known_visit_sim.algorithms.ACBBA:ACBBAAllocator",
        "pi": "known_visit_sim.algorithms.PI:PIAllocator",
        "hipc": "known_visit_sim.algorithms.HIPC:HIPCAllocator",
        "dmchba": "known_visit_sim.algorithms.DMCHBA:DMCHBAAllocator",
        "dga": "known_visit_sim.algorithms.DGA:DGAAllocator",
        "auctiongreedy": "known_visit_sim.algorithms.Auction_greedy:AuctionGreedyAllocator",
        "auction_greedy": "known_visit_sim.algorithms.Auction_greedy:AuctionGreedyAllocator",
    }
    spec = aliases.get(spec.lower(), spec)
    if ":" not in spec:
        raise ValueError(
            "Unknown algorithm. Use CBAA, ACBBA, PI, HIPC, DMCHBA, DGA, "
            "AuctionGreedy, or module.path:ClassName"
        )
    module_name, class_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    if not issubclass(cls, AllocatorBase):
        raise TypeError(f"{spec} is not an AllocatorBase subclass")
    return cls
