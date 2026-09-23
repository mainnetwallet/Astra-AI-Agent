"""Assemble the full Astra stack from one call.

`build()` wires: Store → TaskEngine → Memories →
EventBus → Policy → ToolRegistry(+builtins) → Providers →
AstraRouter (+ Gateway) → ChatPipeline → WorkflowEngine → Scheduler
→ Agent. run.py, tests, and boot helpers all call this, so the wiring is
defined once and verified everywhere.

Chat path: Agent.handle() → ChatPipeline (Gateway understands + assigns →
Provider → Gateway verifies → user). Planner, Executor and Orchestrator
have been deleted; the stack keeps `orchestrator`/`executor` keys (value
None) so anything reading them degrades instead of raising KeyError.

NOTE: the Plugin/Registry system (astra.core.Plugin, astra.core.Registry)
has been removed. `plugins/` is an empty placeholder for future plugins
(see plugins/README.md) — nothing in this module discovers, loads, or
wires plugins anymore.
"""
from __future__ import annotations

import os

from astra.core.config import Config
from astra.core.events import EventBus
from astra.core.permissions import Policy
from astra.core.tasks import TaskEngine
# NOTE: astra.tools.registry.ToolRegistry has been restored (see that
# module's docstring). astra.core.executor (Executor) and
# astra.core.orchestrator (Orchestrator) remain deleted — chat no longer
# goes through them (see astra/ai/chat_pipeline.py). ToolRegistry.execute()
# itself works standalone — see
# tests/test_web3_toolregistry_auto_integration.py.
from astra.ai.router import AstraRouter
from astra.ai.registry import build_providers
from astra.ai.models import ModelRegistry
from astra.ai.discovery import ModelDiscovery
from astra.agents import SPECIALISTS, AgentManager
from astra.browser import BrowserManager, register_browser_tools
from astra.memory.memory import MemorySystem, ExperienceStore
from astra.tools.registry import ToolRegistry
from astra.tools import builtins
from astra.terminal import TerminalManager, register_terminal_tools
from astra.ai.execution_history import AgentExecutionHistory
from astra.workflows.engine import WorkflowEngine
from astra.workflows.scheduler import SchedulerManager
from astra.agent import Agent
from astra.store import Store

def _opt_int(config, key):
    """Optional integer config knob (None when unset/blank) so an unset
    CHAT_MAX_TOKENS lets the provider/model decide instead of a fixed cap."""
    try:
        value = config.get(key)
    except Exception:
        return None
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def default_db_path(config=None) -> str:
    # DATA_DIR is a documented storage-location knob; it must be honoured
    # whether it arrives via the environment, `.env` or `config.json` (the
    # latter two are only visible through Config). Reading os.environ alone
    # silently ignored `DATA_DIR=...` in `.env`.
    data_dir = ""
    if config is not None:
        data_dir = config.get("DATA_DIR", "") or ""
    data_dir = data_dir or os.environ.get("DATA_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    return os.path.join(data_dir, "astra.db")


def build(store: Store | None = None, config=None,
          with_scheduler: bool = False) -> dict:
    """Returns dict with every subsystem wired on one store/config.

      stack = build()
      agent = stack["agent"]; etc.
    """
    config = config or Config()
    if store is None:
        store = Store(config.get("DATABASE") or default_db_path(config))
    store.migrate()

    # core subsystems
    events = EventBus(store)
    # The previous run's in-flight operations are gone for good; close their
    # start rows once so the Activity Log never shows a permanent "… running"
    # operation with no terminal event ever arriving.
    events.reconcile_stale_operations()
    # `system_action` is granted by default because it is what powers the
    # shared Terminal (astra/terminal/) — the one capability the AI Gateway
    # and every Provider need to run a real development workflow. An operator
    # who does not want shell access simply removes it from
    # GRANTED_PERMISSIONS; every terminal tool then fails closed ("denied by
    # policy") exactly like any other ungranted system tool.
    policy = Policy(granted=config.getlist("GRANTED_PERMISSIONS",
                                           default=["read", "low_risk_write",
                                                    "browser_action",
                                                    "system_action"]))
    memory = MemorySystem(store, events)
    experiences = ExperienceStore(store, events)
    tasks = TaskEngine(store, events)

    # tools
    registry = ToolRegistry(policy=policy, events=events, config=config)
    builtins.register_builtins(registry)
    browser_manager = BrowserManager(config=config, events=events, store=store)
    register_browser_tools(registry, browser_manager)
    # Shared Terminal capability: ONE manager, ONE set of terminal tools on
    # the ONE ToolRegistry. The AI Gateway and every Provider reach the same
    # sessions through it — see astra/terminal/.
    terminal_manager = TerminalManager(events=events, config=config,
                                       store=store)
    register_terminal_tools(registry, terminal_manager)

    # Web3 transaction manager: deterministic policy + encrypted keystore.
    # The LLM may only *prepare*; authorize/sign/broadcast stay out of the
    # tool surface. Master secret comes from ASTRA_MASTER_SECRET (or the
    # operator's key file), never from model prompts.
    from astra.web3.transactions import (TransactionManager,
                                         TransactionPolicyEngine)
    from astra.web3.policy import (normalize_mode, normalize_address,
                                   PolicyConfig)
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
    # providers — see astra/ai/gateway.py. It has its own AI connections
    # (Gemini, Groq, Cloudflare, Bedrock) with independent credentials/
    # models/endpoints. It is handed to AstraRouter only as a reference for
    # separate status reporting ("Astra AI Gateway" in health/dashboard
    # output) — AstraRouter never routes or falls back into it, and the
    # Gateway never reads provider config or falls back into ProviderRegistry.
    # Isolation is absolute in both directions.
    from astra.ai.gateway import (build_astra_ai_gateway,
                                  build_gateway_request_intelligence)
    # `store` lets the Gateway persist its own last-successful-target and
    # per-model health across restarts (§14) — the same SQLite database
    # everything else uses, no new one. `events` lets it emit its own
    # "astra_gateway.*" events, kept distinct from the router's "router.*"/
    # "ai.*" events (see astra/core/events.py).
    gateway = build_astra_ai_gateway(config, store=store, events=events)
    router = AstraRouter(providers=providers, config=config, store=store,
                         preference=config.get("AI_ROUTING_PREFERENCE", "balanced"),
                         registry=model_registry, gateway=gateway)
    router.attach_events(events)
    # The Agent Workflow "AI / Agent" node needs a tool on the ONE
    # ToolRegistry, and it can only be registered now that the router
    # exists. It delegates straight to `router.route_request` — no second
    # model client, no second provider list (see astra/tools/ai_tools.py).
    from astra.tools.ai_tools import register_ai_tools
    register_ai_tools(registry, router,
                      default_max_tokens=_opt_int(config, "CHAT_MAX_TOKENS"))
    # Gateway Request Intelligence: rewrites a raw/messy goal into a
    # Provider-ready prompt using ONLY the Gateway's own GW_* connections.
    # Always constructed (never None) — it degrades to a no-op
    # pass-through on its own when `gateway` is None/unusable. See
    # astra/ai/gateway.py module docstring for the isolation contract.
    gateway_intelligence = build_gateway_request_intelligence(gateway)
    discovery = ModelDiscovery(model_registry,
                               adapter_by_name={p.name: p for p in providers})
    env_models = getattr(config, "get", lambda _k, d="": d)("ASTRA_STARTUP_DISCOVERY", "")
    if env_models == "1":
        discovery.refresh(force=False)   # best-effort, never blocks boot

    # specialist agents (Agent manager): deterministic selection steers
    # routing hints; kept registered for introspection (/api/agents-style
    # listings) even though chat no longer plans through them.
    agent_manager = AgentManager()
    agent_manager.register_many(SPECIALISTS)
    # crash recovery for in-flight web3 txs (chain-checked, safe).
    tx_manager.recover()

    # chat pipeline: Gateway understands + assigns -> Provider -> Gateway
    # verifies (fix/redo loop) -> user. The Gateway and the router are the
    # only AI paths; there is no direct/raw LLM path.
    from astra.ai.chat_pipeline import ChatPipeline
    from astra.core.blob_store import BlobStore
    # Full agent/tool execution history persists on the SAME store as
    # everything else — see astra.ai.execution_history / blob_store — and
    # is shared with the workflow ToolContext below so `execution_history_read`
    # works identically from chat and from a workflow step.
    execution_history = AgentExecutionHistory(blobs=BlobStore(store))
    chat_pipeline = ChatPipeline(
        gateway, router, events=events,
        max_tokens=_opt_int(config, "CHAT_MAX_TOKENS"),
        registry=registry, terminal=terminal_manager,
        execution_history=execution_history,
        max_tool_steps=config.getint("CHAT_MAX_TOOL_STEPS", 8),
        agent_brain=config.get("CHAT_AGENT_BRAIN", "provider"))

    # workflows + scheduler
    # ToolContext: shared subsystems a workflow step's tool may legitimately
    # touch (memory, tasks, web3). Without it context-dependent tools fail
    # when run as workflow steps.
    from astra.core.context import ToolContext
    tool_context = ToolContext(store=store, config=config, events=events,
                               memory=memory, tasks=tasks,
                               web3_manager=tx_manager, registry=registry,
                               terminal=terminal_manager,
                               execution_history=execution_history)
    workflows = WorkflowEngine(store, registry, events, context=tool_context)
    scheduler = None
    if with_scheduler:
        scheduler = SchedulerManager(store, workflows, events)
        scheduler.start()

    # chat agent (ChatPipeline -> Astra AI Gateway <-> Provider system;
    # no direct/raw LLM path exists)
    agent = Agent(pipeline=chat_pipeline)

    return {
        "config": config, "store": store,
        "events": events, "policy": policy, "memory": memory,
        "experiences": experiences, "tasks": tasks, "registry": registry,
        "router": router, "executor": None,
        "orchestrator": None, "chat_pipeline": chat_pipeline,
        "workflows": workflows,
        "scheduler": scheduler, "agent": agent,
        "model_registry": model_registry, "provider_registry": provider_registry,
        "discovery": discovery, "agent_manager": agent_manager,
        "browser_manager": browser_manager,
        "terminal": terminal_manager,
        "tx_manager": tx_manager, "keystore": keystore,
        "web3_policy": policy_engine,
        "gateway_intelligence": gateway_intelligence,
    }
