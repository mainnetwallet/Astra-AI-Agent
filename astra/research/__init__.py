"""Astra research package.

`astra/research/lookup.py` holds the stdlib-only quick_lookup helper: it
fetches a URL and extracts its <title>/meta description so the agent can give
a quick "what is this project" summary. It is SSRF-guarded (see
`ASTRA_ALLOW_PRIVATE_URLS`) and reports honestly when offline.
"""
from __future__ import annotations

from .lookup import ResearchReply, extract_url, quick_lookup  # noqa: F401

__all__ = ["ResearchReply", "extract_url", "quick_lookup"]
