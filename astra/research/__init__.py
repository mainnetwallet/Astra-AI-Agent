"""Astra research package.

`astra/research/legacy.py` holds the original quick_lookup helper (kept as a
module-scoped import for the airdrop plugin). `astra/research/engine.py` adds
the richer research engine (search, source rating, comparison, summary).
"""
from __future__ import annotations

from .legacy import ResearchReply, extract_url, quick_lookup  # noqa: F401

__all__ = ["ResearchReply", "extract_url", "quick_lookup"]