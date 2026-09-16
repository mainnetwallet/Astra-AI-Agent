"""Assemble the full Astra stack from one call.

`build()` wires: Store → Registry(plugins) → TaskEngine → Memories →
EventBus → Policy → ToolRegistry(+builtins+plugin tools) → Providers →
AstraRouter → Planner → Executor → WorkflowEngine → Scheduler → Orchestrator
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
from astra.ai.router import AstraRouter
from astra.ai.registry import build_providers, ProviderRegistry
from astra.ai.models import ModelRegistry
from astra.ai.discovery import ModelDiscovery
from astra.agents import SPECIALISTS, AgentManager
from astra.browser import BrowserManager, register_browser_tools
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
                                           default=["read", "low_risk_write",
                                                    "browser_action"]))
    memory = MemorySystem(store, events)
    experiences = ExperienceStore(store, events)
    tasks = TaskEngine(store, events)

    # tools
    registry = ToolRegistry(policy=policy, events=events, config=config)
    builtins.register_builtins(registry)
    browser_manager = BrowserManager(config=config, events=events)
    register_browser_tools(registry, browser_manager)
    registry.register_plugin_tools(plugins)

    # Web3 transaction manager: deterministic policy + encrypted keystore.
    # The LLM may only *prepare*; authorize/sign/broadcast stay out of the
    # tool surface. Master secret comes from ASTRA_MASTER_SECRET (or the
    # operator's key file), never from model prompts.
    from astra.web3.transactions import (TransactionManager,
                                         TransactionPolicyEngine,
                                         PolicyConfig)
    from astra.web3.policy import normalize_mode, normalize_address
    from astra.web3.keystore import SecureKeyStore
    from astra.web3.tools import register_web3_tools
    # Environment → PolicyConfig: every WEB3_* limit/allowlist documented in
    # .env.example must actually reach the runtime policy engine (never just
    # be parsed and discarded). Invalid mode values fail safe to CONFIRM
    # rather than silently enabling AUTO.
    w3_mode = normalize_mode(config.get("WEB3_TRANSACTION_MODE", "CONFIRM"))

    def _addr_set(key: str) -> frozenset:
        return frozenset(normalize_address(a)
                         for a in config.getlist(key, default=[]) if a)

    def _chain_id_set(key: str) -> frozenset:
        out = set()
        for c in config.getlist(key, default=[]):
            try:
                out.add(int(c))
            except (TypeError, ValueError):
                continue
        return frozenset(out)

    w3_cfg = PolicyConfig(
        mode=w3_mode,
        max_tx_value_wei=config.getint("WEB3_MAX_TX_VALUE_WEI", 0),
        max_daily_tx_value_wei=config.getint("WEB3_MAX_DAILY_TX_VALUE_WEI", 0),
        max_gas_limit=config.getint("WEB3_MAX_GAS_LIMIT", 0),
        allowed_recipients=_addr_set("WEB3_ALLOWED_RECIPIENTS"),
        allowed_contracts=_addr_set("WEB3_ALLOWED_CONTRACTS"),
        allowed_wallets=_addr_set("WEB3_ALLOWED_WALLETS"),
        allowed_chain_ids=_chain_id_set("WEB3_CHAIN_IDS"))
    # Master secret: operator-set env, or the key file (sixty-four hex chars).
    import astra.web3.keystore as _ks
    master = os.environ.get("ASTRA_MASTER_SECRET", "") or \
        (open(_ks.DEFAULT_MASTER_KEY_FILE).read().strip()
         if os.path.exists(_ks.DEFAULT_MASTER_KEY_FILE) else "")
    keystore = SecureKeyStore(store, master) if master else None
    policy_engine = TransactionPolicyEngine(w3_cfg)
    tx_manager = TransactionManager(store, keystore=keystore,
                                    policy=policy_engine, events=events,
                                    config=config)
    register_web3_tools(registry, manager=tx_manager)

    # AI providers + router
    # Provider adapters (Gemini/Groq/Mistral/…/Bedrock) are built by the
    # ProviderRegistry from configured credential pools; the legacy Anthropic
    # and OpenAI-compatible providers still join when configured. AstraRouter
    # routes across all of them — it is the routing core, never a provider.
    provider_registry = build_providers(config, events=events)
    providers = provider_registry.all()
    model_registry = ModelRegistry(config)
    # Optional Astra AI Gateway (GW_* config). This is a COMPLETELY SEPARATE
    # system, NOT a provider, and is never added to provider_registry/
    # providers — see astra/ai/gateway.py. It has its own four AI connections
    # (Gemini, Groq, Cloudflare, Bedrock) with independent credentials/
    # models/endpoints. It is handed to AstraRouter only as a reference for
    # separate status reporting ("Astra AI Gateway" in health/dashboard
    # output) — AstraRouter never routes or falls back into it, and the
    # Gateway never reads provider config or falls back into ProviderRegistry.
    # Isolation is absolute in both directions.
    from astra.ai.gateway import build_astra_ai_gateway
    gateway = build_astra_ai_gateway(config)
    router = AstraRouter(providers=providers, config=config, store=store,
                         preference=config.get("AI_ROUTING_PREFERENCE", "balanced"),
                         registry=model_registry, gateway=gateway)
    router.attach_events(events)
    discovery = ModelDiscovery(model_registry,
                               adapter_by_name={p.name: p for p in providers
                                                if getattr(p, "name", "") != "offline"})
    env_models = getattr(config, "get", lambda _k, d="": d)("ASTRA_STARTUP_DISCOVERY", "")
    if env_models == "1":
        discovery.refresh(force=False)   # best-effort, never blocks boot

    planner = Planner(router=router,
                      tools=[t["name"] for t in registry.list()],
                      config=config)
    executor = Executor(registry, tasks=tasks, events=events,
                        experiences=experiences)
    # specialist agents (Agent manager): deterministic selection steers
    # routing hints and plan decoration; the Orchestrator persists the pick.
    agent_manager = AgentManager()
    agent_manager.register_many(SPECIALISTS)
    orchestrator = Orchestrator(
        store, config=config, tasks=tasks, registry=registry,
        planner=planner, executor=executor, router=router, memory=memory,
        experiences=experiences, events=events, policy=policy,
        plugins=plugins, agents=agent_manager)
    orchestrator.web3_manager = tx_manager
    # crash recovery: executions stranded mid-flight by a previous shutdown
    # are marked FAILED so they no longer read as "running".
    orchestrator.recover_stale()
    tx_manager.recover()   # resolve in-flight web3 txs safely (chain-checked)

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
        "model_registry": model_registry, "provider_registry": provider_registry,
        "discovery": discovery, "agent_manager": agent_manager,
        "browser_manager": browser_manager,
        "tx_manager": tx_manager, "keystore": keystore,
        "web3_policy": policy_engine,
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