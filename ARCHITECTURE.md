# Astra AI Agent — Architecture

This document describes how the codebase is actually wired today, verified
against `astra/bootstrap.py` (the single place the whole stack is
assembled) and the source of each subsystem. See `README.md` for the
user-facing quick start; this file is the internals map.

---

## 1. High-level flow

```
                    ┌────────────────────────────────────────────┐
                    │           Web layer (astra/web.py)          │
                    │  auth · rate-limit · CORS · body cap ·      │
                    │  security headers · SSE · request_id        │
                    └───────────────────┬──────────────────────────┘
                                         │
                                         ▼
                              astra/agent.py :: Agent.handle()
                                         │
                                         ▼
                    astra/ai/chat_pipeline.py :: ChatPipeline.run()
                                         │
        ┌────────────────────────────────┼────────────────────────────────┐
        │                                │                                │
        ▼                                ▼                                ▼
  Gateway call #1            Provider execution + AGENT TOOL LOOP   Gateway call #2
  UNDERSTAND + ASSIGN        (AstraRouter.route_request() ->          VERIFY
  (astra/ai/gateway.py)       astra/ai/agent_tool_loop.py ->     (astra/ai/gateway.py)
                               ToolRegistry -> Terminal/files/git)
        │                                │                                │
        └──────────────► bounded fix/redo loop if incomplete ◄────────────┘
                       (astra/ai/gateway_task_completion.py,
                        astra/core/correction.py)
                                         │
                                         ▼
                                    User reply
```

**Every chat message takes this exact path — there is no other AI
execution path.** `tests/test_zero_bypass_hardening.py` is a
source-level regression guard that fails the build if a second path is
ever reintroduced. Key invariants it locks in:

- The Gateway is **never** added to `AstraRouter.providers` /
  `ProviderRegistry`, and is **never** used as a fallback provider when
  every real provider candidate fails. It is not a provider — it is the
  understand/verify layer that wraps provider calls.
- Gateway (`GW_*` env vars) and real Provider (`<PROVIDER>_*` env vars)
  credentials are read from disjoint, non-overlapping env var names.
- Fail-open, never fail-closed on the Gateway: if the Gateway is
  absent/unusable, the message still reaches a provider via the router,
  and the reply is returned with an honest "unverified" note
  (`data.gateway`) rather than an error.

### A note on what used to be here

An earlier **Orchestrator → Planner → ToolRegistry** execution loop
(`UNDERSTAND → PLAN → SELECT TOOL → EXECUTE → OBSERVE → VERIFY → LEARN →
CONTINUE`, with resumable `WAITING_USER` runs) has been **deleted**
(`astra/core/orchestrator.py` and `astra/core/planner.py` no longer
exist). `astra/bootstrap.py` keeps `orchestrator`/`executor` keys in its
returned stack dict (value `None`) purely so old code that reads those
keys degrades instead of raising `KeyError`. `Agent.resume()` reports
honestly that there is nothing to resume any more — chat no longer has
an approve/reject round-trip.

`ToolRegistry` itself was **not** deleted — it still validates, gates,
retries and audits every tool call. It is no longer driven by an
Orchestrator: it is invoked directly by Web3 tool flows, workflow steps,
tests, and — for chat — by the **agent tool loop** described in §1.2,
which lets the AI choose tools (including the shared Terminal) step by
step. There is still exactly one registry and one execution path per tool
call.

### 1.1 Conversation history / memory (multi-turn chat)

Every chat turn is persisted to **`ChatLog`** (`astra/chat_log.py`,
SQLite-backed, one row per message, scoped by `conversation_id`). Before
this fix, that transcript was never read back into a turn: `/api/chat`
read a `context` string straight off the incoming request body, and the
browser never sent one (`static/js/astra.js`), so the Gateway and the
Provider always saw an empty prior conversation — a follow-up like "why?"
or "continue" had nothing to resolve against.

**`ConversationContextBuilder`** (`astra/ai/conversation_context.py`) is
now the single source of prior-turn context for a turn:

```
astra/web.py  (POST /api/chat)
      │  cid = chat_log.current_id
      │  history = context_builder.build(cid)   # BEFORE the current
      │                                          # message is persisted
      ▼
astra/agent.py :: Agent.handle(message, history=..., ...)
      ▼
astra/ai/chat_pipeline.py :: ChatPipeline.run(message, history=..., ...)
      │
      ├──► Gateway call #1 (UNDERSTAND) — sees history as flattened text
      └──► Provider call    — sees the SAME history as real
                              {"role","content"} turns, prepended to
                              the current user message
```

Key properties:

- **Isolation** — `ConversationContextBuilder.build(conversation_id)` only
  ever reads rows for that `conversation_id`; conversations can never mix.
- **No current-message duplication** — `astra/web.py` builds `history`
  from `ChatLog` *before* calling `chat_log.add_user()` for the current
  message, so there is nothing to accidentally echo back. (The builder
  also accepts `exclude_message_id` for callers that persist first.)
- **Same context, both calls** — `ChatPipeline.run(history=...)` builds one
  canonical turn list and hands it to both the Gateway's understand prompt
  and the Provider's `messages[]`, so they can't silently disagree about
  "the conversation so far".
- **No artificial trimming** — history is carried whole by default; there
  is no fixed character/turn ceiling. Provider-aware fitting to the selected
  model's REAL context window happens where the model is known
  (`astra.ai.context_budget`, applied in the router/gateway), and only drops
  the oldest middle turns when the prompt genuinely does not fit. The
  leading system prompt (Core + Gateway execution decision + live
  capability/terminal/execution state + tool protocol) and the current user
  request are structurally protected and are never removed. `CHAT_CONTEXT_MAX_CHARS`
  / `CHAT_CONTEXT_MAX_TURNS` remain optional operator overrides (unset =
  unlimited).
- **Retry/refresh safe** — `ChatLog.add_user()` (and the public
  `is_duplicate_pending()` check `astra/web.py` makes first) detects a
  resubmission of the exact same text while that conversation's turn is
  still in flight and skips both the duplicate log row and a second
  pipeline run, rather than producing two persisted replies.
- **Separate from long-term memory** — `MemorySystem` (§6) is a distinct
  subsystem (working/short/long/semantic/episodic layers) and is never
  used as a substitute for thread history; `ConversationContextBuilder`
  only ever reads `ChatLog`.

---

### 1.2 Shared Terminal + agent tool loop (development workflow)

Astra can now run a real, multi-step development workflow — the same shape
Claude Code / Codex use — without a second execution path:

```
AI call -> decide: use a tool, or answer
        -> ToolRegistry.execute(...)          (the ONE registry)
        -> structured result back into the SAME AI execution
        -> AI call again ... until it answers
```

- **One shared Terminal.** `astra/terminal/` owns the single terminal
  capability: `TerminalSession` (persistent cwd/env/history, background
  processes, timeout, stop/kill, capped streaming output) and
  `TerminalManager` (session registry + lifecycle). `astra/terminal/tools.py`
  registers `terminal_exec`, `terminal_start`, `terminal_status`,
  `terminal_stop`, `terminal_kill`, `terminal_history`, `terminal_sessions`
  and `terminal_close` on the SAME `ToolRegistry` as the file/git/browser/
  web3 tools. There is no provider-specific terminal and no parallel tool
  framework.
- **Both brains can call it.** `AgentToolLoop`
  (`astra/ai/agent_tool_loop.py`) is brain-agnostic: `ProviderToolCaller`
  drives it through `AstraRouter` (the chat default) and
  `GatewayToolCaller` drives it through the Gateway's own connections
  (`AstraAIGateway.run_tool_loop`; `AstraRouter.run_tool_loop` is the
  provider-side entry). Both reach the identical `ToolRegistry`, so both
  reach the identical terminal sessions. `CHAT_AGENT_BRAIN=provider|gateway`
  selects which one drives a chat turn (default `provider`); corrections for
  a gateway-driven turn go back to the Gateway (`_GatewayPort`).
- **Structured results, dynamic next step.** Every command returns
  `{command, cwd, session_id, process_id, shell, status, exit_code, stdout,
  stderr, duration}`. A failed command is data, not a crash, so the AI can
  diagnose, edit and retry. No command sequence is hard-coded. Models
  without native tool-calling use a tiny JSON protocol; a plain-text reply
  is treated as final, so ordinary chat is unchanged.
- **Three separate histories.** Conversation history (`ChatLog` /
  `ConversationContextBuilder`) is untouched; agent/tool execution history
  (`astra/ai/execution_history.py`) records what was attempted; terminal
  session state lives in the session. Before each Gateway/Provider call the
  pipeline composes a *bounded, deterministic* view of the execution +
  terminal context and gives it to BOTH. Nothing replays unlimited output.
- **Isolation and lifecycle.** A conversation's terminal session id is
  `conv-<conversation_id>`, so unrelated chats never share cwd, processes or
  history. A caller with no conversation id gets a request-scoped
  `req-<request_id>` session that is closed when the turn ends (an embedder
  that wants continuity without a conversation id passes an explicit
  `session_id`), so unrelated callers never share terminal state by
  accident. Sessions are closed (and their process trees killed) on shutdown
  (`web_fastapi.py` lifespan), on `terminal_close`, and by `close_idle`.
- **Events.** `terminal.started`, `terminal.output` (capped snippets),
  `terminal.completed`, `terminal.failed`, `terminal.timeout`,
  `terminal.stopped`, plus `agent.tool_loop.*` / `agent.tool_call` /
  `agent.tool_result`, all correlated by `op`/`trace` in the Activity Log.

Permission model: terminal tools are `SYSTEM_ACTION` risk and run without a
per-command prompt so an autonomous loop is possible; the real gate is the
operator's `GRANTED_PERMISSIONS` list (`system_action` is granted by default
in `bootstrap.py` — remove it and every terminal tool fails closed).

---

### 1.3 Gateway capability context + structured execution handoff

The Gateway is the **request-understanding / planning / orchestration brain**;
the Provider is the **execution AI**; the `ToolRegistry` is the **single
source of truth** for what the runtime can actually do; `AgentToolLoop` is the
**actual tool-execution loop**; Gateway verification is the **completion
gate**. One chat turn therefore looks like:

```
User
 -> Gateway UNDERSTAND
      - inspects the LIVE runtime capability catalog (derived from the
        actual ToolRegistry on this turn, never hardcoded)
      - preserves the user's exact intent (final_request)
      - decides whether real tool execution is required, and which
        capability category performs it
      - emits a structured execution handoff:
        {"execution": {"required": true, "capability": "terminal",
                       "intent": "..."}}
 -> Provider / AgentToolLoop
      - receives the SAME live ToolRegistry, the Gateway's execution
        decision, the exact tool catalog + argument schemas, the
        conversation history, and the live terminal/execution state
      - actually executes (ToolRegistry.execute -> terminal_exec -> ...)
      - the structured tool result returns into the SAME conversation
 -> Gateway VERIFY
      - an execution-required task is COMPLETE only when real execution
        evidence exists (tool call, tool result, exit code, terminal state,
        execution history) — a nicely worded "here's how you could do it"
        answer is INCOMPLETE
      - if incomplete, the correction is re-dispatched through the SAME
        AgentToolLoop for execution-required tasks, so a correction can
        actually run the required tool instead of producing more prose
 -> final natural-language response (internal protocol/metadata never leak)
```

**One capability representation, two consumers.**
`astra.ai.capability_context.collect_runtime_capabilities(registry)` builds a
single `RuntimeCapabilities` value from the live `ToolRegistry` on every
turn. It exposes:

- `human_context` — the clean, human-facing capability block (categories
  only, no tool names, no protocol). Handed to the Gateway's UNDERSTAND
  prompt *and* to the Provider's runtime context.
- `catalog_text()` — the exact machine-facing capability IDs
  (`terminal`, `files`, `browser`, ...) the Gateway may use for
  `execution.capability`.

`astra.ai.agent_tool_loop.build_tool_catalog(registry)` remains the separate,
machine-facing **exact tool catalog** the model uses to actually invoke a
tool (exact names + argument schemas), appended by the tool loop as
`TOOL_PROTOCOL`. The two artifacts are deliberately distinct: a capability
question is answered from `human_context`, tool execution is driven by
`build_tool_catalog`. There is no second registry and no hand-maintained
Gateway-side capability list — both read the same live `ToolRegistry`.

**Structured handoff.** The Gateway's UNDERSTAND reply may carry an
`execution` object; `astra.ai.gateway_contract.ProviderExecutionDecision`
parses it, and `normalized(live_categories)` guarantees the Gateway can never
demand a capability the runtime does not have (an unknown category is
dropped; no tool capability at all forces `required=false`). The decision
travels to the Provider in the system-prompt runtime context *and* as the
first context block of the tool loop, so it survives provider failover — a
fallback model sees the identical requirement, tools and tool protocol.

**Verification uses execution evidence.** For an execution-required task,
`ChatPipeline` builds the completion contract with
`evidence_required=("tool_execution",)`, backed by a live callable that reads
`AgentExecutionHistory` (what the agent actually did). The deterministic gate
runs before the semantic verifier, so an answer produced without the required
tool action can never be verified COMPLETE. `ProviderExecutionDecision` is
also shown to the verifier, which is explicitly told an execution task is not
complete from a description/promise. Corrections for such tasks go through
`_ToolLoopPort`, which re-enters the same `AgentToolLoop` — the same
`ToolRegistry`, the same shared Terminal, the same `CHAT_AGENT_BRAIN`.

**Secrets.** Tool results are redacted (`astra.security.redact_text`) before
they re-enter the model conversation or the execution history, the terminal
context block is redacted before it is injected into any prompt, and
`sanitize_final_response` remains the last gate before the reply reaches the
user.

### 1.4 Token and context policy (no artificial API caps)

Astra no longer imposes small fixed completion or context caps. Two modules
own the policy, and every Gateway/Provider request goes through them:

- `astra.ai.token_limits` — output length. OpenAI-compatible HTTP and
  Bedrock Converse treat the output-token field as **optional**, so an unset
  budget is *omitted* and the model uses its own maximum. Anthropic's
  Messages API **requires** the field, so an unset budget is *derived from
  the selected model's capability* (`Model.max_output_tokens`, else its
  `context_window`), never a universal small number. An explicit budget
  (`CHAT_MAX_TOKENS`, or a caller-passed value) is honoured verbatim.
- `astra.ai.context_budget` — input length. `fit_messages()` keeps the full
  prompt when it fits the target model's real `context_window`, and only
  drops the **oldest middle turns** when it does not. Messages are sized as
  an explicitly-labelled *estimate* (no tokenizer is bundled); the window it
  is compared against is real model metadata. Reduction priority is: Core
  system instructions → Gateway execution decision → current user request →
  tool protocol/schema → live terminal/execution state → recent tool results
  → recent conversation → oldest history; the first five live in the system
  prompt (or are the final message) and can never be dropped.

Both apply inside `AstraRouter._attempt` (the single Provider choke point, so
the AgentToolLoop's growing multi-step conversation is fitted on every step)
and inside `AstraAIGateway.chat`/`stream` (including the tool-loop Gateway
calls). Because fitting is done per **selected** target, a provider failover
preserves the complete useful context subject only to the fallback model's
own window — never a smaller generic budget. Terminal resource controls
(process timeout, process-tree cleanup, the stdout/stderr memory buffer,
`CHAT_MAX_TOOL_STEPS`) are separately retained: those are resource safety,
not AI context limits.

## 2. Directory map

```
astra/
├── agent.py            Chat entry point (Agent.handle/resume/dashboard/...)
├── bootstrap.py         Wires the entire stack — the one source of truth
├── web_fastapi.py       ASGI adapter: turns web.py's Request/Response into
│                        real bytes; one catch-all route
├── web.py               The actual route table, auth, rate limit, CORS,
│                        SSE, security headers — server-framework-neutral
├── chat_log.py          Server-side chat transcript (survives refresh)
├── store.py             Generic SQLite storage (plain dicts, no ORM)
├── security.py          Secret redaction, SSRF guard, rate limiter, request_id
│
├── ai/                  Everything AI: providers, router, Gateway, pipeline
│   ├── provider.py       AIProvider base + backward-compatible Claude /
│   │                     generic OpenAI-compatible providers
│   ├── adapters/          10 concrete provider adapters (see §3)
│   ├── registry.py        Builds the provider list from env config
│   ├── models.py          Model registry (capabilities, context, cost class)
│   ├── token_limits.py    Provider-aware OUTPUT token resolution (§1.4)
│   ├── context_budget.py  Provider-aware INPUT/context fitting (§1.4)
│   ├── discovery.py        Model discovery / refresh
│   ├── credentials.py      Per-provider credential pools (health, cooldown)
│   ├── router.py           AstraRouter — scores + routes + rotates creds
│   ├── routing_policy.py    Task-type → provider/model scoring rules
│   ├── gateway.py           Astra AI Gateway — 4 independent GW_* connections
│   ├── gateway_contract.py  Execution port/result types shared with router
│   ├── gateway_task_completion.py  Bounded verify → correct → re-verify loop
│   ├── gateway_recovery.py   Recovery semantics for interrupted gateway tasks
│   ├── capability_context.py ONE live RuntimeCapabilities read (human
│   │                        capability block + exact capability IDs)
│   ├── gateway_contract.py   ProviderExecutionTarget/Result/Decision — the
│   │                        Gateway <-> Provider handoff contract
│   ├── gateway_routing.py    Gateway-side provider short-name mapping
│   ├── gateway_supervision.py Supervises a task end-to-end through the loop
│   ├── chat_pipeline.py    The single path every chat message takes (§1)
│   ├── agent_tool_loop.py    Iterative AI tool loop (Gateway- or
│   │                         Provider-brained) over the shared ToolRegistry
│   ├── execution_history.py  Bounded agent/tool execution history, scoped
│   │                         per conversation (separate from ChatLog)
│   ├── capabilities.py      Capability tags (chat/streaming/tools/vision/...)
│   ├── multimodal_messages.py  Builds multimodal message payloads
│   ├── artifact_extraction.py  Detects + extracts code/doc artifacts from replies
│   └── json_extract.py      Lenient JSON extraction from model output
│
├── terminal/            ONE shared persistent Terminal for the Gateway AND
│                        every Provider/model (via the shared ToolRegistry)
│   ├── session.py         TerminalSession — cwd/env/history, background
│   │                      processes, timeout, stop/kill, streaming
│   ├── manager.py          TerminalManager — session registry + lifecycle
│   └── tools.py            terminal_exec/start/status/stop/kill/history/...
│
├── agents/              Specialist agents (task-type skill buckets; NOT
│                        plugins, NOT providers — see §4)
│   ├── base.py            SpecialistAgent base class
│   ├── manager.py          AgentManager — scores + selects a specialist
│   ├── general.py, research.py, browser.py, coding.py, files.py,
│   │   web3.py, airdrop.py   One file per specialist
│
├── core/                Generic, domain-agnostic infrastructure
│   ├── config.py          Env/`.env` config reader (get/getlist/getint)
│   ├── events.py           EventBus — persisted events + SSE feed
│   ├── permissions.py       Tool risk levels (READ ... SYSTEM_ACTION) + policy
│   ├── tasks.py             Generic DAG task engine (used by workflows)
│   ├── state.py             Shared state-machine constants
│   ├── correction.py         Bounded fix/redo attempt counting for the Gateway
│   ├── classification.py     Task-type classification helpers
│   ├── context.py            Request-scoped context plumbing
│   ├── artifacts.py          Artifact (generated file) bookkeeping
│   ├── attachments.py        Uploaded-file handling
│   ├── exceptions.py         ProviderError, TimeoutError, ...
│   └── store_migrations.py   SQLite schema migrations
│
├── tools/               Tool definitions + the registry that runs them
│   ├── registry.py         ToolRegistry — validation, gating, retry, audit
│   ├── builtins.py          14 core tools (memory/tasks/web/files/health)
│   ├── document_gen.py      generate_document tool implementation
│   └── schemas.py           Shared JSON-schema fragments for tool inputs
│
├── web3/                Wallet / transaction subsystem
│   ├── tools.py            5 tools: token_balance, chain_status, rpc_status,
│   │                       tx_prepare, tx_status
│   ├── policy.py            Deterministic CONFIRM/AUTO policy engine
│   ├── chains.py            Chain/RPC config + failover list
│   ├── keystore.py           Secure private-key storage (never leaves it)
│   ├── rpc.py                RPC call plumbing
│   ├── signer.py             Transaction signing
│   └── transactions.py, raw_tx.py   Transaction lifecycle + persistence
│
├── browser/              Optional Playwright-backed browsing subsystem
│   ├── __init__.py         register_browser_tools(): browser_open/observe/
│   │                        action/extract/screenshot/close
│   ├── manager.py          BrowserManager — per-manager session map (locked);
│   │                        close_all() releases Playwright on shutdown
│   └── sessions.py         BrowserSession — page lifecycle, bounded observe,
│                            SSRF-guarded open, CAPTCHA/MFA pause
│
├── memory/               Layered memory + learned experience
│   └── memory.py           MemorySystem (working/short/long/semantic/
│                            episodic) + ExperienceStore
│
├── research/             stdlib URL/metadata lookup helper
│   └── lookup.py            quick_lookup / extract_url (SSRF-guarded)
│
├── workflows/             Multi-step, resumable workflows
│   ├── engine.py            WorkflowEngine — steps reference
│   │                        {{step_id.param}}, run through ToolRegistry
│   └── scheduler.py          SchedulerManager — oneshot/interval/daily/
│                             weekly/deadline triggers, tick-thread daemon

plugins/                  Empty placeholder — see §4
static/                   SPA frontend (vanilla JS + CSS, served by web.py)
                          js/log_model.js — Activity Log timeline model
tests/                    unittest + pytest suite (see §7)
```

---

## 3. AI providers

There are two provider families. Both are assembled by
`astra/ai/registry.py::build_providers()` and both are routable peers of the
AstraRouter.

**Modern adapters (recommended).** Ten modules under `astra/ai/adapters/`:
nine share one `CompatibleAdapter` base (`astra/ai/adapters/base.py`) that
speaks OpenAI-style `/chat/completions`, and `bedrock.py` is a real AWS-SigV4 /
Converse adapter. Each takes an unlimited credential pool and is seeded into
the model registry from its `<PROVIDER>_MODELS` env var:

| Provider | Adapter file | Env (keys) | Env (models) |
|---|---|---|---|
| Gemini | `adapters/gemini.py` | `GEMINI_API_KEYS` | `GEMINI_MODELS` |
| Groq | `adapters/groq.py` | `GROQ_API_KEYS` | `GROQ_MODELS` |
| Mistral | `adapters/mistral.py` | `MISTRAL_API_KEYS` | `MISTRAL_MODELS` |
| OpenRouter | `adapters/openrouter.py` | `OPENROUTER_API_KEYS` | `OPENROUTER_MODELS` |
| Cerebras | `adapters/cerebras.py` | `CEREBRAS_API_KEYS` | `CEREBRAS_MODELS` |
| Cloudflare | `adapters/cloudflare.py` | `CLOUDFLARE_API_KEYS` (+`CLOUDFLARE_ACCOUNT_IDS`) | `CLOUDFLARE_MODELS` |
| SambaNova | `adapters/sambanova.py` | `SAMBA_API_KEYS` | `SAMBA_MODELS` |
| Cohere | `adapters/cohere.py` | `COHERE_API_KEYS` | `COHERE_MODELS` |
| Z.ai | `adapters/zai.py` | `ZAI_API_KEYS` | `ZAI_MODELS` |
| Bedrock | `adapters/bedrock.py` | `BEDROCK_API_KEYS` (bearer) or `BEDROCK_CREDENTIALS` (`access_key:secret_key`) | `BEDROCK_MODELS` |

Each provider gets an unlimited **credential pool**
(`astra/ai/credentials.py`): round-robin/least-recently-used selection
across healthy keys, per-key cooldown on failure, and an auth failure
marks only that one key unhealthy so sibling keys keep working. Secrets
never leave the `Credential` object — only non-secret metadata (id,
healthy, calls, errors) is exposed to dashboards/events.

**Backward-compatible providers (legacy).** `astra/ai/provider.py` also
defines the two original single-provider paths. They are registered only when
configured, and still route through the same router, but they are not part of
the modern adapter set:

| Provider | Class | Env | Notes |
|---|---|---|---|
| Anthropic | `ClaudeProvider` (name `anthropic`) | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | one key, no credential pool |
| OpenAI-compatible | `OpenAICompatibleProvider` (name `openai` by default; `AI_PROVIDER_LABEL` renames it) | `AI_BASE_URL`, `AI_MODEL`, `AI_API_KEY` | OpenAI, Ollama, LM Studio, DeepSeek… |

They have no credential pool and are not seeded from the `<PROVIDER>_MODELS`
registry env vars (the router still builds model metadata for them on the fly,
and discovery can list their models). New setups should use the adapters above.

`AstraRouter` (`astra/ai/router.py`) sits on top: it classifies the
task type, scores candidate provider/model pairs by health + past
outcomes, rotates credentials, and records routing stats
(`/api/v1/router/status`, `/api/v1/router/stats`).

The separate **Astra AI Gateway** (`astra/ai/gateway.py`) is four
independent connections (`GW_GEMINI_*`, `GW_GROQ_*`,
`GW_CLOUDFLARE_*`, `GW_BEDROCK_*`) with its own fallback chain (Gemini
→ Groq → Cloudflare → Bedrock). It does not import or wrap the Provider
adapter classes — it's a fully separate implementation used only for
the Gateway's own UNDERSTAND/VERIFY calls in the chat pipeline (§1),
never as a fifth "provider" in the router's candidate list.

**System prompt architecture.** Every AI/model call in this section (and
in §1's tool loop) is composed the same way:

```
ASTRA_CORE_SYSTEM_PROMPT          (astra/ai/system_prompt.py)
        +
task/role-specific instructions   (UNDERSTAND, CLASSIFY, VERIFY, PROVIDER,
                                    TOOL_PROTOCOL — each module's own
                                    specialized layer)
        +
relevant runtime context          (history, terminal/execution state,
                                    current task — sent as separate
                                    message(s), never baked into the
                                    system prompt itself)
```

`astra.ai.system_prompt.build_system_prompt()` is the single place the
Core prompt (identity, operating principles, tool-use/hallucination/
recovery/internal-output rules shared by every call) gets attached to a
specialized prompt. The per-turn runtime context for both the Gateway's
UNDERSTAND call and the Provider includes the LIVE capability block (and, for
an execution-required task, the structured execution decision) — see §1.3. Each `*_SYSTEM_PROMPT` constant in `chat_pipeline.py`
and `gateway.py` is built through it once at import time; provider
adapters (`astra/ai/adapters/*`) never inject it themselves and stay
provider-agnostic. `AgentToolLoop.run()` (§1) guarantees the Core layer is
present exactly once before appending `TOOL_PROTOCOL`, whether the caller
already passed a Core-wrapped prompt (the normal case) or a bare
specialized one — see `tests/test_system_prompt.py` for the full
regression coverage (every call site receives Core, specialized layers
remain intact, no duplication across retries/corrections, no leak into
the user-facing reply).

---

## 4. Specialist agents (not plugins)

`astra/agents/manager.py::AgentManager` holds a set of
`SpecialistAgent` subclasses and picks the best-scoring one for a goal
(`select(goal, task_type)`). Agents are declarative skill buckets — a
`name`, a `description`, and which tools/task-types they're suited for.
They are **not** AI providers and **not** a plugin system.

| Agent | File | Role |
|---|---|---|
| `general` | `general.py` | Conversation, planning, explanation, delegation (fallback) |
| `research` | `research.py` | Search the web, fetch pages, summarize |
| `browser` | `browser.py` | Open sites, observe, click/fill/scroll |
| `coding` | `coding.py` | Inspect codebases, edit/create files, run tests |
| `files` | `files.py` | Read/write/edit/search files (text, JSON, CSV, ...) |
| `web3` | `web3.py` | Wallet balances, ERC-20 tokens, chain/RPC health |
| `airdrop` | `airdrop.py` | Airdrop campaigns, eligibility tasks, deadlines |

**There is no separate plugin loader.** `astra.core.Plugin`/`Registry`
was removed from the codebase; `plugins/` is an empty folder
(`plugins/README.md` says so explicitly) and `ACTIVE_PLUGINS` is a no-op
legacy key read only for the public-config `plugins` count (0 by default).
Adding a new capability currently means adding a specialist agent (+ its
tools) in `astra/agents/` and `astra/tools/`, not dropping a file into
`plugins/`.

---

## 5. Web3 / transaction safety

`astra/web3/` implements a deterministic, non-LLM-controlled money path:

- **Tools** (`web3/tools.py`): `token_balance`, `chain_status`,
  `rpc_status` are pure reads. `tx_prepare` can only *prepare* a
  transaction — it never signs. `tx_status` checks a prepared tx's
  lifecycle by id.
- **Policy** (`web3/policy.py`): a `TransactionPolicyEngine` evaluates
  every prepared tx against sender/recipient/contract allowlists,
  per-tx and daily value caps, gas limit, and chain allowlist — wired
  from `WEB3_*` env vars in `bootstrap.py`.
- **Modes**:
  - `CONFIRM` (default) — every tx parks at `PREPARED` for the operator
    to approve (`POST /api/v1/web3/transactions/{tx_id}/authorize`) or
    reject (`.../reject`). Both require `ASTRA_TOKEN`. `reject` only
    accepts a tx still `PREPARED`/`VALIDATED`; an unknown or
    already-decided id returns a structured 400, not a fake success.
  - `AUTO` — only txs that pass the deterministic policy send
    automatically, for user-authorized wallets; anything outside policy
    is never auto-approved.
- The LLM can prepare a tx but can **never** change mode, policy, or
  add wallets — only the operator (with `ASTRA_TOKEN`) can, via the web
  API. `tx_prepare` is the one tool with a `confirmation_delegate`
  (`"web3_tx"`) so its approve/deny path is owned end-to-end by the
  policy engine instead of the generic ask-gate every other
  `FINANCIAL_ACTION` tool uses.
- Never-sign-twice is enforced with a deterministic tx hash + MAC; an
  uncertain tx is resolved by querying the chain by hash, never
  blindly resubmitted. Private keys stay in the secure keystore and
  never appear in logs, events, API responses, or plaintext DB rows.

---

## 6. Cross-cutting infrastructure

- **`ToolRegistry`** (`tools/registry.py`) — every tool call goes
  through schema validation, a permission-level gate
  (`core/permissions.py`: `READ` → `LOW_RISK_WRITE` → `BROWSER_ACTION`
  → `FINANCIAL_ACTION` → `SYSTEM_ACTION`), timeout/retry/backoff, and
  an audit trail. Each executed call also emits `tool.started` then one
  terminal `tool.completed`/`tool.failed` event (tool name, category,
  duration, and a redacted input/output summary) for the Activity Log.
  Both events carry the same `op` correlation id and the terminal event
  is flagged `terminal=True`, so the panel resolves one row instead of
  leaving a stale "… running" row. Runs standalone today (see §1) —
  Web3 flows and `tests/test_web3_toolregistry_auto_integration.py`
  call it directly.
- **`MemorySystem`** (`memory/memory.py`) — five layers (working,
  short, long, semantic, episodic), all SQLite-backed, no secrets, no
  embeddings (deterministic keyword scoring). `ExperienceStore` learns
  from past task outcomes before repeating a similar goal.
- **`WorkflowEngine` + `SchedulerManager`** (`workflows/`) — a workflow
  is a list of steps naming a registered tool, with dependencies and
  `{{step_id.param}}` data flow between steps; persisted so runs can be
  paused/resumed/audited. The scheduler fires workflows on
  oneshot/interval/daily/weekly/deadline triggers via a tick-thread
  daemon (no external cron). Every step is executed with the same
  shared `ToolContext` the API tools use, so context-dependent tools
  (`remember`/`recall`/`create_task`/…) work inside a workflow too.
- **`EventBus`** (`core/events.py`) — every subsystem publishes here;
  events persist to SQLite (audit trail + Activity Log history) and
  stream to the frontend over SSE (`/api/v1/events/stream`).
  `reconcile_stale_operations()` runs once per start (from
  `bootstrap.py`) to close operations the previous process left running —
  see "Interrupted operations" under the Activity Log.
- **`TaskEngine`** (`core/tasks.py`) — a generic DAG task engine that
  workflows dispatch through; a failed child task fails only itself,
  the workflow decides whether to abort or continue.
- **Security** (`security.py`, enforced in `web.py`) — global secret
  redaction (api keys/tokens/seeds/passwords/private keys stripped from
  every outbound response + SSE frame), SSRF-safe URL checks
  (`ASTRA_ALLOW_PRIVATE_URLS` opt-out), per-IP rate limiting, structured
  API errors, and a request id on every response.
- **`Store`** (`store.py`) — one small SQLite wrapper, thread-safe via
  a single `RLock` (uvicorn runs blocking route work on a worker
  threadpool), plain dicts, no ORM.

---

## 7. Web layer

`astra/web.py` owns *what* a request means (route table, auth, rate
limit, body cap, CORS, security headers, SSE framing, JSON envelopes)
against neutral `Request`/`Response` objects — it has no ASGI/Starlette
import. `astra/web_fastapi.py` is the only adapter that turns those into
real bytes via **one catch-all route**, so no individual route can ever
drift out of sync between the two layers. Every route is served under
both `/api/...` (legacy) and `/api/v1/...` (same handler).

The app owns its own lifecycle (starts/stops the scheduler, closes the
store) only when `bootstrap.build()` handed it a full `stack`; tests and
embedding code can instead pass `site=`/`store=`/`agent=` directly and
keep ownership themselves — that's how the test suite boots a real
server in-process on an ephemeral port.

### Activity Log panel

The panel is a *view* over the existing `/api/events` history and
`/api/events/stream` SSE feed — there is no second logging system. Rows
render oldest → newest, ordered by the backend event timestamp
(`created_at`, id as the same-second tiebreak) rather than arrival order;
an out-of-order event is inserted at its real position, and history and
the live feed use the same comparator. Live events normally append at the
bottom, and the scroll position drives follow mode: near the bottom it
auto-scrolls and keeps the newest row visible, scrolling up stops auto-follow and shows a
"↓ New logs" pill that jumps back and resumes. The DOM is capped at 300
rows and the in-memory dedupe/pause buffers are bounded, so a long-lived
panel cannot grow without limit. `AstraLog.isMeaningful()` drops
heartbeats (`scheduler.tick`, `ai.token`) and the Gateway's duplicate
`ai.*` mirror of an `astra_gateway.*` call; everything else is mapped by
`AstraLog.normalize()` to a timeline row (icon, title, subject, status,
duration) with expandable, redacted details. Every visible row carries a
real state — RUNNING / COMPLETE / FAILED / WARNING — so no row renders
the old meaningless "•" `info` fallback. The pure mapping + scroll
state machine live in `static/js/log_model.js` (unit-tested under node);
`static/js/astra.js` only does DOM work.

**Lifecycle reconciliation.** Start and terminal events are paired by a
reliable correlation id carried on the event payload — `op` (tool, AI,
router, gateway, chat-pipeline, workflow and workflow-step operations),
`tx` (Web3 transactions) or `task_id` (the generic task engine) — never
by title. `AstraLog.planRender()` decides whether an arriving event
appends a new row or updates the row for its operation in place, so a
`started → completed/failed/cancelled/timeout` pair (including retry
updates) resolves to a single row and concurrent operations of the same
kind each keep their own row. An update refines the row in place: its
displayed time and timeline position stay pinned to the original start
event, so completion order never changes the visual order. A finished
operation shows the full span (`23:21:19 → 23:22:07`) and a duration —
the explicit `duration_ms`/`latency_ms` when present, otherwise derived
from the two backend timestamps — and the expandable details list
Started/Completed/Duration/Status plus the `op`/`trace` correlation ids.
The run's own row is never closed by one of its steps (only descendants
are), so a workflow run stays a single row. A terminal event also closes
the still-running children it owns (matched by the request `trace` /
`run_id`), marking them `warn`/interrupted; every chat turn, workflow
run and tool call emits such a terminal event, so a finished request
never leaves an operation stuck on "running".

Internal steps that belong to an operation carry that operation's `op`
rather than creating a row of their own: the router's `router.fallback`
and `router.gateway_task_completion` / `router.gateway_supervision`
progress events, and the Gateway recovery reports
(`gateway.execution_completed/_recovered/_failed`, `gateway.target_cooldown`)
all refine the same "Agent Router" row. The Gateway's assignment step
(`chat.pipeline.assigned`) keeps its own, distinctly labelled row
("Agent assigned") so it is never confused with the router's row.

**Interrupted operations.** `ChatPipeline.run()` always emits a terminal
event for the turn, even when a step raises unexpectedly. If the process
itself dies mid-operation (server restart, crash, aborted request) no
terminal can be written; on the next start
`EventBus.reconcile_stale_operations()` scans the recent history and
closes every `op`-correlated start with no terminal by emitting one
synthetic `operation.interrupted` (`terminal=True`,
`original_kind=<the start kind>`), so the row keeps its title and shows
`⚠ interrupted when the app stopped` with start → interruption time.
Children whose request already has a terminal are skipped there (that
terminal resolves them), which keeps the reconciliation idempotent.

---

## 8. Tests

`tests/` uses the stdlib `unittest` runner plus `pytest` for the live
server smoke test. Notable files:

- `test_zero_bypass_hardening.py` — the architectural regression guard
  described in §1 (no second AI execution path, Gateway never used as
  fallback provider, disjoint credential namespaces).
- `test_gateway_*.py` — routing, recovery, supervision, task-completion
  behavior of the Gateway/ChatPipeline loop.
- `test_web3.py`, `test_web3_policy_wiring.py`,
  `test_web3_toolregistry_auto_integration.py` — policy engine + tool
  wiring for the transaction subsystem.
- `test_fastapi_server.py` — boots the real FastAPI/uvicorn server
  in-process and checks status codes, security headers, JSON bodies,
  static bytes, SSE frames, lifespan, and the worker pool.
- `test_tool_events.py` — the `tool.started`/`tool.completed`/
  `tool.failed` lifecycle contract (one terminal event per call,
  shared `op` correlation id, redacted input/output, a broken bus never
  breaks a tool).
- `test_lifecycle_terminal_events.py` — every chat-pipeline turn and
  workflow step/run start is closed by a terminal event carrying the
  same correlation id (no stale "running" rows).
- `test_log_model_js.py` + `tests/js/log_model.test.js` — runs the pure
  Activity Log mapping/scroll/pause/filter logic under node (skipped
  when node is absent); `test_activity_log_ui.py` locks the static
  panel contract (anchors, chips, append-not-prepend, no HTML injection).
- `test_security.py`, `test_per_key_health.py`,
  `test_provider_error_isolation.py` — redaction, credential health,
  and failure-isolation guarantees.

Run the full suite with `python3 -m unittest discover -s tests -v`;
`python3 -m pytest -q tests/test_fastapi_server.py` for the live smoke
test alone.
