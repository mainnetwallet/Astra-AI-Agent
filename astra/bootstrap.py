"""Assemble the full Astra stack from one call.

`build()` wires: Store → Registry(plugins) → TaskEngine → Memories →
EventBus → Policy → ToolRegistry(+builtins+plugin tools) → Providers →
AgentRouter → Planner → Executor → WorkflowEngine → Scheduler → Orchestrator
→ Agent. run.py, tests, and boot helpers all call this, so the wiring is
defined once and verified everywhere.
"""
from __future__ import annotations

import importlib
import os

from astra.core import Registry
from astra.core.config import Config
from astra.core.events import EventBus
from astra.core.permissions import Policy
from astra.core.tasks import TaskEngine
from astra.core.planner import Planner
from astra.core.executor import Executor
from astra.core.orchestrator import Orchestrator
from astra.ai.provider import (ClaudeProvider, OpenAICompatibleProvider,
                              OfflineProvider)
from astra.ai.router import AgentRouter
from astra.memory.memory import MemorySystem, ExperienceStore
from astra.tools.registry import ToolRegistry
from astra.tools import builtins
from astra.workflows.engine import WorkflowEngine
from astra.workflows.scheduler import SchedulerManager
from astra.agent import Agent
from astra.store import Store

PLUGIN_MODULES = ["plugins.airdrop"]


def _discover_plugins(reg: Registry, modules: list[str]) -> None:
    for mod_name in modules:
        mod = importlib.import_module(mod_name)
        plugin_cls = getattr(mod, "Plugin", None) or getattr(mod, "PLUGIN", None)
        if plugin_cls is None:
            from astra.core import Plugin as PluginBase
            plugin_cls = next(v for v in vars(mod).values()
                              if isinstance(v, type) and v.__module__ == mod.__name__
                              and issubclass(v, PluginBase))
        reg.add(plugin_cls)


def default_db_path() -> str:
    data_dir = os.environ.get("DATA_DIR", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
    return os.path.join(data_dir, "astra.db")


def build(store: Store | None = None, config=None, with_plugins: bool = True,
          with_scheduler: bool = False,
          plugin_modules: list | None = None) -> dict:
    """Returns dict with every subsystem wired on one store/config.

      stack = build()
      agent = stack["agent"]; plugins = stack["plugins"]; etc.
    """
    modules = plugin_modules or PLUGIN_MODULES
    config = config or Config()
    if store is None:
        store = Store(config.get("DATABASE", default_db_path()))
    store.migrate()

    # plugins
    reg = Registry()
    if with_plugins:
        _discover_plugins(reg, modules)
    plugins = reg.load(store, config)

    # core subsystems
    events = EventBus(store)
    policy = Policy(granted=config.getlist("GRANTED_PERMISSIONS",
                                           default=["read", "low_risk_write"]))
    memory = MemorySystem(store, events)
    experiences = ExperienceStore(store, events)
    tasks = TaskEngine(store, events)

    # tools
    registry = ToolRegistry(policy=policy, events=events, config=config)
    builtins.register_builtins(registry)
    registry.register_plugin_tools(plugins)

    # AI providers + router
    # Precedence for a working provider: Anthropic key → OpenAI-compatible
    # (OpenAI/OpenRouter/Ollama/local via AI_BASE_URL + AI_API_KEY) → offline.
    # AI_PROVIDER env can force a subset, e.g. "openai" (visibility only —
    # the router still tries what is configured).
    providers = []
    _key = _first_environ("ANTHROPIC_API_KEY")
    if _key:
        providers.append(ClaudeProvider(config=config, api_key=_key, events=events))
    if config.get("AI_BASE_URL") or _first_environ("AI_API_KEY"):
        providers.append(OpenAICompatibleProvider(config=config, events=events))
    providers.append(OfflineProvider(config))
    router = AgentRouter(providers=providers, config=config)

    planner = Planner(router=router,
                      tools=[t["name"] for t in registry.list()],
                      config=config)
    executor = Executor(registry, tasks=tasks, events=events,
                        experiences=experiences)
    orchestrator = Orchestrator(
        store, config=config, tasks=tasks, registry=registry,
        planner=planner, executor=executor, router=router, memory=memory,
        experiences=experiences, events=events, policy=policy,
        plugins=plugins)
    # crash recovery: executions stranded mid-flight by a previous shutdown
    # are marked FAILED so they no longer read as "running".
    orchestrator.recover_stale()

    # workflows + scheduler
    workflows = WorkflowEngine(store, registry, events)
    scheduler = None
    if with_scheduler:
        scheduler = SchedulerManager(
            store, workflows, events,
            deadline_callback=lambda: _deadline_events(plugins))
        scheduler.start()

    # chat agent (plugins first, then orchestrator)
    agent = Agent(plugins, llm=None, orchestrator=orchestrator)

    return {
        "config": config, "store": store, "plugins": plugins,
        "events": events, "policy": policy, "memory": memory,
        "experiences": experiences, "tasks": tasks, "registry": registry,
        "router": router, "planner": planner, "executor": executor,
        "orchestrator": orchestrator, "workflows": workflows,
        "scheduler": scheduler, "agent": agent,
    }


def _deadline_events(plugins) -> list:
    """Used by the scheduler: airdrop deadlines as (name, iso-date)."""
    out = []
    for p in plugins:
        if hasattr(p, "upcoming_deadlines"):
            try:
                for a in p.upcoming_deadlines(30):
                    if a.get("deadline"):
                        out.append((a.get("name", "?"), a["deadline"]))
            except Exception:
                continue
    return out


def _first_environ(key: str) -> str:
    for name in (key, "ASTRA_" + key, key.upper(), "ASTRA_" + key.upper()):
        if os.environ.get(name):
            return os.environ[name]
    return ""