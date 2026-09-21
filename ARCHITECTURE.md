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
  Gateway call #1                 Provider execution              Gateway call #2
  UNDERSTAND + ASSIGN        (via AstraRouter.route_request())        VERIFY
  (astra/ai/gateway.py)        (astra/ai/router.py + adapters)   (astra/ai/gateway.py)
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
retries and audits every tool call. It just isn't driven by an
Orchestrator any more; today it's invoked directly (Web3 tool flows,
workflow steps, tests).

---

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
│   ├── discovery.py        Model discovery / refresh
│   ├── credentials.py      Per-provider credential pools (health, cooldown)
│   ├── router.py           AstraRouter — scores + routes + rotates creds
│   ├── routing_policy.py    Task-type → provider/model scoring rules
│   ├── gateway.py           Astra AI Gateway — 4 independent GW_* connections
│   ├── gateway_contract.py  Execution port/result types shared with router
│   ├── gateway_task_completion.py  Bounded verify → correct → re-verify loop
│   ├── gateway_recovery.py   Recovery semantics for interrupted gateway tasks
│   ├── gateway_routing.py    Gateway-side provider short-name mapping
│   ├── gateway_supervision.py Supervises a task end-to-end through the loop
│   ├── chat_pipeline.py    The single path every chat message takes (§1)
│   ├── capabilities.py      Capability tags (chat/streaming/tools/vision/...)
│   ├── multimodal_messages.py  Builds multimodal message payloads
│   ├── artifact_extraction.py  Detects + extracts code/doc artifacts from replies
│   └── json_extract.py      Lenient JSON extraction from model output
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
render oldest → newest with live events appended at the bottom, and the
scroll position drives follow mode: near the bottom it auto-scrolls and
keeps the newest row visible, scrolling up stops auto-follow and shows a
"↓ New logs" pill that jumps back and resumes. The DOM is capped at 300
rows and the in-memory dedupe/pause buffers are bounded, so a long-lived
panel cannot grow without limit. `AstraLog.isMeaningful()` drops
heartbeats (`scheduler.tick`, `ai.token`) and the Gateway's duplicate
`ai.*` mirror of an `astra_gateway.*` call; everything else is mapped by
`AstraLog.normalize()` to a timeline row (icon, title, subject, status,
duration) with expandable, redacted details. The pure mapping + scroll
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
kind each keep their own row. A terminal event also closes the
still-running children it owns (matched by the request `trace` /
`run_id`), marking them `warn`/interrupted; every chat turn, workflow
run and tool call emits such a terminal event, so a finished request
never leaves an operation stuck on "running".

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
