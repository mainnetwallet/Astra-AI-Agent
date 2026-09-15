"""Shared harness for Astra tests. Import as `from helpers import ...`
(discover -s tests puts this directory on sys.path)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.store import Store              # noqa: E402
from astra.agent import Agent              # noqa: E402
from plugins.airdrop import AirdropPlugin  # noqa: E402


def make_agent():
    """Returns (store, plugin, agent) wired exactly like the real app."""
    store = Store(":memory:")
    plugin = AirdropPlugin(store)
    agent = Agent([plugin])
    return store, plugin, agent


def make_plugin():
    store = Store(":memory:")
    return AirdropPlugin(store)


def make_stack(**kw):
    """Full stack (orchestrator, tools, memory, workflows, scheduler, …) on a
    fresh in-memory store — the same wiring run.py uses."""
    from astra.bootstrap import build
    return build(store=Store(":memory:"), **kw)