from __future__ import annotations

import ctypes
import os


def commit_fraction() -> float:
    """Return Windows commit usage, or zero when unavailable."""
    if os.name != "nt":
        return 0.0

    class PerformanceInformation(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("CommitTotal", ctypes.c_size_t),
            ("CommitLimit", ctypes.c_size_t),
            ("CommitPeak", ctypes.c_size_t),
            ("PhysicalTotal", ctypes.c_size_t),
            ("PhysicalAvailable", ctypes.c_size_t),
            ("SystemCache", ctypes.c_size_t),
            ("KernelTotal", ctypes.c_size_t),
            ("KernelPaged", ctypes.c_size_t),
            ("KernelNonpaged", ctypes.c_size_t),
            ("PageSize", ctypes.c_size_t),
            ("HandleCount", ctypes.c_ulong),
            ("ProcessCount", ctypes.c_ulong),
            ("ThreadCount", ctypes.c_ulong),
        ]

    value = PerformanceInformation()
    value.cb = ctypes.sizeof(value)
    if not ctypes.windll.psapi.GetPerformanceInfo(
        ctypes.byref(value),
        value.cb,
    ):
        return 0.0
    if not value.CommitLimit:
        return 0.0
    return float(value.CommitTotal) / float(value.CommitLimit)


def require_safe_commit(limit: float = 0.70) -> float:
    fraction = commit_fraction()
    if fraction >= limit:
        raise RuntimeError(
            f"host commit guard: {fraction:.1%} used (limit {limit:.0%})"
        )
    return fraction
