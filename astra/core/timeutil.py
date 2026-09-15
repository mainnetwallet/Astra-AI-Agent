"""Tiny time helpers shared across astra.core.

duration_ms(now → perf_counter) gives integer milliseconds elapsed.
"""
from __future__ import annotations

import time


def duration_ms(t0: float) -> int:
    """Integer milliseconds since `t0` (perf_counter timestamp)."""
    return int((time.perf_counter() - t0) * 1000)


def ms_now() -> float:
    return time.perf_counter()