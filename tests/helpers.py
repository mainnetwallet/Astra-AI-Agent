"""Shared harness for Astra tests. Import as `from helpers import ...`
(discover -s tests puts this directory on sys.path).
No plugins are registered — plugins/ is empty pending future additions
(see plugins/README.md)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.store import Store              # noqa: E402
from astra.agent import Agent              # noqa: E402


def make_agent():
    """Returns (store, plugins, agent) wired exactly like the real app."""
    store = Store(":memory:")
    plugins = []
    agent = Agent()
    return store, plugins, agent


def make_plugin():
    """No plugins are registered yet (see plugins/README.md)."""
    return None


def make_stack(**kw):
    """Full stack (orchestrator, tools, memory, workflows, scheduler, …) on a
    fresh in-memory store — the same wiring run.py uses."""
    from astra.bootstrap import build
    return build(store=Store(":memory:"), **kw)