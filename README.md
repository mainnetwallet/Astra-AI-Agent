# Astra AI Agent 🚀

A **local Personal AI OS** — a core that routes every chat message through a
verification gateway, remembers what happens, and ships task-specialist agents,
tool registry, web3 wallet safety and a live web UI. Python 3.9+; the only
runtime dependencies are **FastAPI** and **uvicorn**, which serve the web API
and the single-page frontend.

Astra's chat path: every message goes through the **Astra AI Gateway**
(understands the request, assigns the best provider/model, then verifies the
answer before it reaches you) before a real AI provider — reached through the
**AstraRouter** — produces it. Domain-specific work (airdrop tracking, web3,
browsing, coding, files, research) ships as built-in **specialist agents**
(`astra/agents/`), but chat no longer *plans* through them: like the plugin
system, they are registered for introspection and routing hints only.

**Gateway = understanding/planning/orchestration · Provider = execution AI ·
`ToolRegistry` = the one source of truth for real capabilities/tools ·
`AgentToolLoop` = the actual tool-execution loop · Gateway verification =
completion verification.** The Gateway is handed the LIVE runtime capability
catalog (derived from the actual `ToolRegistry` on every turn — never
hardcoded, never a second list) and emits a structured execution handoff
(`{"execution": {"required": true, "capability": "terminal", "intent": ...}}`)
when a request needs a real tool. The Provider/AgentToolLoop receives that
decision plus the exact machine-facing tool catalog (names + argument
schemas), the conversation history and the live terminal/execution state, and
runs the tool through the SAME registry. An execution task is only verified
COMPLETE when real execution evidence exists (tool call, tool result, exit
code, terminal state), and a correction to such a task re-enters the tool
loop so the required tool actually executes instead of the model describing
how the user could do it. See ARCHITECTURE.md §1.3.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Astra AI Agent — Personal AI OS                                         │
│                                                                          │
│  User message ──▶ Agent.handle() ──▶ ChatPipeline.run()                  │
│                                              │                           │
│                 ┌────────────────────────────┼─────────────────────────┐ │
│                 ▼                            ▼                         ▼ │
│        Gateway call #1                 Provider execution        Gateway call #2
│        UNDERSTAND + ASSIGN          (AstraRouter → adapters)         VERIFY
│        (astra/ai/gateway.py)                                  (bounded fix/redo loop)
│                 │                            │                         │
│                 └──────▶ reply to user ◀─────┴─────────────────────────┘ │
│                                                                          │
│  ┌─────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐ │
│  │ AgentManager│ │   Memory     │ │  Workflows   │ │  ToolRegistry    │ │
│  │ (specialist │ │ (layered)    │ │ + Scheduler  │ │ (builtin + web3  │ │
│  │  selection) │ │              │ │  (DAG)       │ │  + browser)      │ │
│  └─────────────┘ └──────────────┘ └──────────────┘ └──────────────────┘ │
│                                                                          │
│  ┌──────────────────────┐   ┌─────────────────────────────────────────┐ │
│  │ Web3 Transaction     │   │ Security layer                          │ │
│  │ Manager + deterministic   │ auth · rate-limit · CORS · request_id  │ │
│  │ policy (CONFIRM/AUTO)│   │ redaction · SSRF guard · body cap       │ │
│  └──────────────────────┘   └─────────────────────────────────────────┘ │
│                                                                          │
│  Store: SQLite · server: FastAPI/uvicorn (ASGI) · SSE · UI: vanilla SPA  │
└──────────────────────────────────────────────────────────────────────────┘
```

The Gateway is a **separate, isolated system** with its own ten AI
connections (`GW_*` config: Gemini, Groq, Cloudflare, Bedrock, OpenRouter,
Mistral, Cerebras, SambaNova, Cohere, Z.AI). It is *not* a provider and is
never in the provider registry; if it is unusable the pipeline **fails open**
— the message still reaches a provider via the router and the reply is
returned with an
honest "unverified" note. There is exactly one AI execution path; an earlier
Orchestrator/Planner loop was removed, and `ToolRegistry` now runs standalone
(workflow steps, web3 flows and tests call it directly).

Gateway connections (each independently optional — configure only the ones
you have keys for; unconfigured ones are simply absent):

| Connection | `GW_*` env prefix | Official endpoint | Default models |
|---|---|---|---|
| Gemini | `GW_GEMINI_` | `generativelanguage.googleapis.com/v1beta/openai` | `gemini-3.5-flash`, `gemini-3.1-flash-lite` |
| Groq | `GW_GROQ_` | `api.groq.com/openai/v1` | `openai/gpt-oss-120b` |
| Cloudflare | `GW_CLOUDFLARE_` (+ `_ACCOUNT_IDS`) | `api.cloudflare.com/client/v4` | `@cf/meta/llama-3.3-70b-instruct-fp8-fast` |
| Bedrock | `GW_BEDROCK_` (+ `_CREDENTIALS`, `_REGION`) | `bedrock-runtime.<region>.amazonaws.com` | full Bedrock catalog (`us.amazon.nova-lite-v1:0` default) |
| OpenRouter | `GW_OPENROUTER_` | `openrouter.ai/api/v1` | `nvidia/nemotron-3-*:free` |
| Mistral | `GW_MISTRAL_` | `api.mistral.ai/v1` | `mistral-small-2603` |
| Cerebras | `GW_CEREBRAS_` | `api.cerebras.ai/v1` | `gpt-oss-120b` |
| SambaNova | `GW_SAMBANOVA_` (or `GW_SAMBA_`) | `api.sambanova.ai/v1` | `Meta-Llama-3.3-70B-Instruct` |
| Cohere | `GW_COHERE_` | `api.cohere.ai/compatibility/v1` | `command-a-03-2025` |
| Z.AI (GLM) | `GW_ZAI_` | `api.z.ai/api/paas/v4` | `glm-4.7-flash` |

Each connection takes a comma-separated key pool and a model list
(`GW_<NAME>_API_KEYS` / `GW_<NAME>_MODELS`), and each model id can be
overridden per install. All ten share one contract: Bearer auth from the
connection's own pool, per-key health/cooldown, 60s (120s streaming)
timeouts, bounded transient retries (`GW_MAX_RETRIES`), HTTP
error/rate-limit classification, failover to the next healthy target, and
normalized OpenAI-style responses with usage metadata when the provider
reports it.

### Key subsystems

| Subsystem | What it does |
|-----------|-------------|
| **Astra AI Gateway + ChatPipeline** | The path every chat message takes: Gateway call #1 understands the request, reads the LIVE runtime capability catalog, decides whether real tool execution is required (structured `execution` handoff) and assigns the best provider/model; the provider executes through the **agent tool loop** (inspect → terminal → read/edit → test → retry) using the shared ToolRegistry; Gateway call #2 verifies the output against real execution evidence and drives a bounded fix/redo loop if it is incomplete — re-entering the tool loop for execution tasks so a correction can actually run the required tool. Fails open. |
| **AstraRouter** (`astra/ai/router.py`) | Scores task types, ranks provider/model candidates by health+score, rotates credentials, learns from outcomes and records routing stats. Never registered as a provider itself, and the Gateway is never a fallback provider when every real provider fails. |
| **Provider adapters (10)** (`astra/ai/adapters/`) | Gemini, Groq, Mistral, OpenRouter, Cerebras, Cloudflare, SambaNova, Cohere, Z.ai, Bedrock. Nine share an OpenAI-compatible adapter base; Bedrock is a real AWS SigV4 / Converse adapter. Unlimited credentials per provider via key pools. |
| **Backward-compatible providers** (`astra/ai/provider.py`) | The two original single-provider paths: `ClaudeProvider` (`ANTHROPIC_API_KEY`) and `OpenAICompatibleProvider` (`AI_BASE_URL`/`AI_API_KEY`). Routed when configured; legacy, no key pool. |
| **Model registry** (`astra/ai/models.py`) | Capabilities, context window, streaming/tools/vision support, cost/speed/quality classes, preferred/disabled status. |
| **AgentManager + specialists** (`astra/agents/`) | Declarative task specialists (`general`, `research`, `browser`, `coding`, `files`, `web3`, `airdrop`) the manager scores; not providers and not plugins. Chat does not plan through them today. |
| **ToolRegistry** (`astra/tools/registry.py`) | Builtin + web3 + browser + **terminal** tools with schema validation, permission policy, timeout/retry, audit trail. The single tool surface the AI Gateway, every Provider/model, the workflow engine and the agent tool loop all execute through. |
| **Shared Terminal** (`astra/terminal/`) | ONE persistent terminal for the whole system: durable cwd/env/history per session, background processes, timeout/stop/kill, capped streaming output, and per-conversation isolation. Exposed as `terminal_*` tools on the ToolRegistry, so the Gateway and every Provider drive the identical implementation. |
| **Agent tool loop** (`astra/ai/agent_tool_loop.py`) | The iterative AI loop: the model decides the next action (tool or answer), tools run through the shared registry, structured results feed back into the SAME execution, repeat until done. Brain-agnostic — `AstraRouter.run_tool_loop` (provider) or `AstraAIGateway.run_tool_loop` (gateway). |
| **MemorySystem** (`astra/memory/memory.py`) | Layered memory (working/short/long/semantic/episodic) with importance scoring + search; `ExperienceStore` learns from past outcomes. |
| **WorkflowEngine + SchedulerManager** (`astra/workflows/`) | Step workflows with `{{step_id.param}}` data flow, run on demand or on oneshot/interval/daily/weekly/deadline triggers (no external cron). Steps run with the shared `ToolContext`, so context-dependent tools (`remember`, `recall`, `create_task`, …) work as steps, not just direct tool calls. |
| **EventBus** (`astra/core/events.py`) | Persisted events + SSE streaming to the Activity Log tab; closes operations interrupted by a previous run at startup. |
| **TaskEngine** (`astra/core/tasks.py`) | Generic DAG task engine workflows dispatch through. |
| **Web3 Manager** (`astra/web3/`) | Lifecycle-tracked transactions (CREATED→…→CONFIRMED/FAILED), deterministic CONFIRM/AUTO policy, never-sign-twice, on-chain recovery. Private keys never leave the secure keystore. |

---

## Quick Start

```bash
git clone https://github.com/mainnetwallet/Astra-AI-Agent.git
cd Astra-AI-Agent
bash setup.sh
bash start.sh
# Open http://localhost:8787/
```

`setup.sh` installs the two runtime dependencies (FastAPI + uvicorn). No
database setup. Python 3.9+.

Windows: double-click `setup.bat` then `start.bat` (or `setup.ps1`/`start.ps1`).
Linux / macOS / Termux: the same `setup.sh` + `start.sh` flow.

> Windows prerequisite: Astra itself runs on Windows, but **Agent work runs
> inside WSL2 Ubuntu** (see *Astra Agent Runtime* below). Install it once
> with `wsl --install -d Ubuntu` and Astra will use it. Astra never installs
> WSL for you, and Windows CMD/PowerShell are never used for Agent
> execution.

> Termux/Android: pydantic 2 has no Android wheel. `setup.sh` detects Termux
> and installs the pure-Python path
> (`pip install "fastapi<0.119" "uvicorn>=0.27" "pydantic<2"`).

> ⚠️ **Security default:** the server binds **127.0.0.1** (loopback). Add
> `ASTRA_TOKEN=...` to your `.env` (or environment) before opening it to your
> LAN with `BIND=0.0.0.0` — every `/api/*` call then requires the token.

---

## Setting up AI providers

Copy `.env.example` to `.env` and fill in at least one provider. Astra ships
two provider families; everything configured joins the same router as a peer.

### Modern adapters (recommended)

Ten adapter modules live in `astra/ai/adapters/`. Nine share one
OpenAI-compatible base; `bedrock.py` is a real AWS SigV4 / Converse adapter.
Each reads a space/comma-separated **key pool** — unlimited credentials,
rotated per request — plus an optional model list:

| Provider | Adapter module | Env (keys) | Env (models, optional) |
|----------|----------------|-----------|------------------------|
| Gemini | `astra/ai/adapters/gemini.py` | `GEMINI_API_KEYS` | `GEMINI_MODELS` |
| Groq | `astra/ai/adapters/groq.py` | `GROQ_API_KEYS` | `GROQ_MODELS` |
| Mistral | `astra/ai/adapters/mistral.py` | `MISTRAL_API_KEYS` | `MISTRAL_MODELS` |
| OpenRouter | `astra/ai/adapters/openrouter.py` | `OPENROUTER_API_KEYS` | `OPENROUTER_MODELS` |
| Cerebras | `astra/ai/adapters/cerebras.py` | `CEREBRAS_API_KEYS` | `CEREBRAS_MODELS` |
| Cloudflare | `astra/ai/adapters/cloudflare.py` | `CLOUDFLARE_API_KEYS` (+`CLOUDFLARE_ACCOUNT_IDS`) | `CLOUDFLARE_MODELS` |
| SambaNova | `astra/ai/adapters/sambanova.py` | `SAMBA_API_KEYS` | `SAMBA_MODELS` |
| Cohere | `astra/ai/adapters/cohere.py` | `COHERE_API_KEYS` | `COHERE_MODELS` |
| Z.ai | `astra/ai/adapters/zai.py` | `ZAI_API_KEYS` | `ZAI_MODELS` |
| Bedrock | `astra/ai/adapters/bedrock.py` | `BEDROCK_API_KEYS` (bearer token) **or** `BEDROCK_CREDENTIALS` (`access_key:secret_key` IAM pairs) | `BEDROCK_MODELS` |

Bedrock's region comes from `BEDROCK_BASE_URL` / `AWS_REGION` (default
`us-east-1`).

### Backward-compatible providers (legacy)

`astra/ai/provider.py` also defines the two original single-provider paths.
They are still routed when configured, but they are the legacy path — no key
pool and no model-registry seeding:

| Provider | Class | Env | Notes |
|----------|-------|-----|-------|
| Claude / Anthropic | `ClaudeProvider` | `ANTHROPIC_API_KEY` (+`ANTHROPIC_MODEL`) | one Anthropic key |
| Any OpenAI-compatible endpoint | `OpenAICompatibleProvider` | `AI_BASE_URL` / `AI_MODEL` / `AI_API_KEY` (+`AI_PROVIDER_LABEL`) | OpenAI, Ollama, LM Studio, DeepSeek… |

All keys stay in env/`.env` — never in the DB, logs, UI or API responses.
`AI_PROVIDER` is an optional **preference / opt-in list** (`AI_PROVIDER=gemini
groq …`); it does not pick a single default provider, and there is no such
default any more — with none set, the router auto-selects among everything
configured.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 8787 | HTTP server port |
| `BIND` | 127.0.0.1 | Bind address (secure local default) |
| `ASTRA_TOKEN` | *(none)* | Operator token — protects every `/api/*` + `/api/v1/*` (`ASTRA_API_KEY` is an alias) |
| `ENV` | development | `production` hides error details |
| `ASTRA_API_RATE_LIMIT` | 300 | Per-IP requests/minute |
| `ASTRA_MAX_BODY_MB` | 50 | Max request body size |
| `ASTRA_CORS_ORIGINS` | *(reflect in dev)* | Allowlisted origins (space-separated) |
| `ASTRA_ALLOW_PRIVATE_URLS` | 0 | SSRF guard override for URL research |
| `AI_PROVIDER` | *(auto)* | Force/opt-in a provider order (`AI_PROVIDER=gemini groq`) |
| `AI_ROUTING_PREFERENCE` | fastest | AstraRouter scoring preference — fastest healthy model is tried first, unhealthy/rate-limited providers are skipped, with instant fallback to the next-fastest on failure |
| `AI_MAX_RETRIES` | 2 | Retries per provider before failing over to the next |
| `AI_BACKOFF` | 1.0 | Base seconds for exponential retry backoff |
| `CHAT_MAX_TOKENS` | *(unset)* | Optional explicit output budget per chat turn. Unset = provider/model decides (no Astra-imposed cap). |
| `CHAT_CONTEXT_MAX_CHARS` | *(unset)* | Optional history character ceiling. Unset = no limit; provider-aware fitting uses the selected model's real context window. |
| `CHAT_CONTEXT_MAX_TURNS` | *(unset)* | Optional history turn ceiling. Unset = no limit. |
| `ASTRA_STARTUP_DISCOVERY` | 0 | Run model discovery once at boot |
| `GRANTED_PERMISSIONS` | `read low_risk_write browser_action system_action` | Tool permission levels granted to the agent (`system_action` powers the shared Terminal; remove it to fail terminal tools closed) |
| `CHAT_MAX_TOOL_STEPS` | 8 | Max tool calls per chat turn before the agent tool loop stops |
| `CHAT_AGENT_BRAIN` | provider | Which AI drives the tool loop: `provider` (AstraRouter) or `gateway` (Gateway's own connections). Both use the same shared Terminal. |
| `ASTRA_TERMINAL_SHELL` | *(auto-detect)* | Override the terminal shell (e.g. `/bin/sh`, `pwsh`); auto-detects bash/sh, PowerShell/cmd and Termux |
| `TERMINAL_MAX_OUTPUT_CHARS` | 20000 | Resource-safety buffer cap for stdout/stderr held in the terminal session. Not an AI context limit — full output reaches the model while its context window allows. |
| `TERMINAL_HISTORY_LIMIT` | 50 | Commands retained in each terminal session's history |
| `HOST_APPROVAL_TTL_S` | 900 | Seconds a host-terminal fallback approval card stays valid before it expires (Allow/Deny only; never runs on expiry) |
| `DATA_DIR` | ./data | Runtime data directory (SQLite DB + `uploads/`) |
| `DATABASE` | `<DATA_DIR>/astra.db` | Explicit SQLite file path |
| `ASTRA_WORKSPACE` | `./workspace` | Root the file tools may read/write (path escapes rejected) |
| `BROWSER_SCREENSHOTS` | `data/screenshots` | Directory browser-tool screenshots are written to |
| `ASTRA_MASTER_SECRET` | *(key file)* | Web3 keystore master secret |
| `WEB3_TRANSACTION_MODE` | CONFIRM | `CONFIRM` (manual) or `AUTO` (policy-only) |
| `WEB3_MAX_TX_VALUE_WEI` / `WEB3_MAX_DAILY_TX_VALUE_WEI` / `WEB3_MAX_GAS_LIMIT` | 0 (unlimited) | Deterministic policy limits |
| `WEB3_ALLOWED_RECIPIENTS` / `WEB3_ALLOWED_CONTRACTS` / `WEB3_ALLOWED_WALLETS` | — | Space-separated allowlists |
| `WEB3_CHAIN_IDS` | 1 8453 137 56 42161 10 11155111 | Chain whitelist |
| `NO_BROWSER` | unset | Set to `1` to not auto-open the browser on start |
| `ASTRA_SCHEDULER` | 0 | Start the scheduler daemon |
| `ASTRA_FASTAPI_DOCS` | 0 | Expose `/docs` + `/openapi.json` |
| `ASTRA_ASGI_THREADS` | 0 (Starlette's default of 40) | Worker-thread pool used for blocking route work |

## Astra Agent Runtime & Astra Agent Terminal

Astra runs Agent work inside its own isolated Linux environment — the
**Astra Agent Runtime** — and gives you a real PC-style terminal onto it,
the **Astra Agent Terminal**.

* **Real isolation, not a simulation.** The runtime is a separate Linux
  filesystem and process tree provided by a per-platform backend:
  **proot + proot-distro** on Android/Termux and **WSL2 + Ubuntu** on
  Windows. On Android only the runtime's own `/workspace`, `/root` and
  `/tmp` are bound in, plus `/dev`, `/proc` and `/sys` - the host home, the
  Termux prefix and `/sdcard` are invisible. On Windows the session runs in
  a private mount namespace in which every Windows-provided mount (`/mnt/c`,
  `C:\`, the WSL system mounts) is unmounted, so the Windows filesystem is
  not reachable from a command. On both platforms the guest environment is
  built from scratch (`env -i`) so host PATH, credentials and profile cannot
  leak in.
* **`cmd.exe` and PowerShell are NOT the Agent Runtime.** On Windows the
  chain is Astra Terminal -> Astra Runtime -> WSL2 -> Ubuntu -> `bash`.
  The Agent runs `bash` *inside Ubuntu* and only ever sees
  `astra:/workspace$`; `wsl.exe` is used purely as transport and is never
  exposed to the Agent as a capability. If WSL2/Ubuntu is missing the
  runtime reports itself **unavailable** with a one-time install hint
  (`wsl --install -d Ubuntu`) - it never falls back to a Windows shell.
* **No silent host fallback.** If the runtime is unavailable or genuinely
  cannot perform an operation, execution stops and the UI says *Agent
  Runtime unavailable* — nothing silently runs on your host shell. The
  legacy host `terminal_exec` family is marked `agent_forbidden` and
  hard-blocked in `ToolRegistry.execute` for every Agent/Provider/workflow
  call — never advertised to a model, never spawned.
* **Host fallback needs your explicit Allow — in the Assistant Chat.** The
  runtime is always tried first and needs no permission. When (and only
  when) the runtime genuinely cannot do the job and a host command would
  help, the Agent calls `host_terminal_request`, which executes *nothing*:
  it posts a scoped approval card into the Assistant Chat (`⚠ Host Terminal
  Access Required`) showing the exact command, cwd and reason. The host
  command runs only if you press **Allow** — exactly once; **Deny** (or
  letting it expire) never runs it. Approval lives *only* in the chat: the
  Astra Agent Terminal is a pure terminal and never shows Allow/Deny.
  API: `POST /api/terminal/approval`, `GET /api/terminal/approval/<id>`,
  `GET /api/terminal/approvals`, `POST /api/terminal/approval/<id>`.
* **Per-runtime private state by default.** `PYTHONUSERBASE` / `NPM_CONFIG_PREFIX`
  / `CARGO_HOME` / `GOPATH` / `GEM_HOME` / `XDG_*` are redirected into each
  runtime's own `$HOME`, so a package installed in Runtime A is importable
  from A (and after a restart of A) but invisible to Runtime B — even though
  the distro rootfs is shared.
* **A real terminal, not a dashboard.** The pane is xterm.js over a genuine
  PTY (`pty.fork()`), so prompts, arrows, `Tab`, `Ctrl+C/D/L/Z/A/E/W/R`, ANSI
  colours, full-screen programs, scrollback, selection and real
  `TIOCSWINSZ` resize all work. Tabs are real sessions — new, switch,
  rename, close, reconnect. The viewport owns nearly the whole screen: one
  header line, tabs, one status line and a `⋮` overflow menu for the file
  drawer and runtime actions. On mobile a compact Termux-style extra-key
  row (`ESC TAB CTRL ALT / - HOME END ↑ ↓ ← → PGUP PGDN`) appears and the
  app height follows the software keyboard.
* **Chat and terminal share one session.** The chat agent and the terminal
  both use `conv-<id>`. Run `git clone` in chat, open the terminal, run
  `ls` — you see the clone, in the same shell, with the same cwd.
* **Files and packages stay in the runtime.** Upload/import files, extract
  `.zip`/`.tar.gz`/`.tgz` (traversal, absolute paths and escaping symlinks
  are rejected), and install with `npm`, `pip`, `apt`, `apk` or `git` —
  always inside the runtime, and always **verified** (a zero exit code on
  its own is not treated as proof).
* **Lifecycle you control.** `runtime_create/start/stop/restart/reset/destroy`
  as tools and in the UI. Normal chat completion never destroys the runtime:
  your projects, dependencies and Git repos persist.

Full detail — isolation model, lifecycle, security guards, events, the
tool list and troubleshooting — is in **[docs/AGENT_RUNTIME.md](docs/AGENT_RUNTIME.md)**.
Configuration keys are in `.env.example` under *Astra Agent Runtime*.

Run the real end-to-end acceptance check on your own machine with
`python3 scripts/runtime_acceptance.py` (drives the real runtime - proot on
Android/Termux, WSL2 Ubuntu on Windows:
PTY, package install + verification, restart persistence, Runtime A/B
private state, host isolation, shared chat↔terminal session, and the
host-`terminal_exec` block).

## Web3 transaction safety

* **CONFIRM** (default): every transaction parks at `PREPARED` for your review —
  the LLM can only *prepare* a send, never authorise or sign. The operator
  approves via `POST /api/v1/web3/transactions/{tx_id}/authorize` (signs +
  broadcasts) or rejects via `.../reject` — both require `ASTRA_TOKEN`.
* **AUTO**: only transactions that pass the *deterministic* policy (sender /
  recipient / contract allowlist, max per-tx, daily budget, gas, chain) send
  automatically, for user-authorised wallets. A tx outside AUTO policy is never
  auto-approved. The LLM can **never** change mode, policy or wallets — only the
  operator (with `ASTRA_TOKEN`) can.
* Never-sign-twice is enforced with a deterministic tx hash + MAC; an uncertain
  tx is resolved by querying the chain by hash, never blindly re-submitted.
  Private keys stay in the encrypted keystore and never appear in logs, events,
  API responses or plaintext DB rows.

## Web UI tabs

The **🖥️ Agent Terminal** tab is a real terminal onto the Agent Runtime (see above); the **📡 Activity Log** tab stays a separate lifecycle/audit timeline.

| Tab | What it shows |
|-----|-------------|
| **Dashboard** | Overview cards — empty placeholder (no plugin registers data today) |
| **Assistant** | Chat interface, with multi-chat history persisted server-side |
| **AI Providers health** | Provider health, latency, calls/errors, per-key and per-model tests |
| **Router** | Model registry + task routing stats (the AstraRouter's view) |
| **Wallet** | Web3 transaction policy (mode, limits, allowlists) + recent txs |
| **Backup** | Export/import as one JSON file — currently a placeholder (`_exports: {}`) |
| **Activity Log** | Live execution timeline (SSE): chronological by backend event time, newest at the bottom (out-of-order events land in place), auto-follow with "↓ New logs", category filters (Agents/AI/Tools/Browser/Web3/Errors), search, pause/resume, copy, clear, expandable details. Start/terminal events are reconciled by a correlation id, so a running row is updated in place — keeping its original timestamp/position — instead of leaving a stale entry; internal router/gateway progress events refine their operation's row (no duplicates), every row shows a real RUNNING/COMPLETE/FAILED/WARNING state, and a request interrupted by a restart is closed as `⚠ interrupted` on the next start |

## API

Every route works under `/api/...` (legacy) **and** `/api/v1/...` (the same
handler; the `v1` segment is stripped before dispatch). `?token=` is accepted
for SSE, where headers cannot be sent.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | DB / providers / scheduler diagnostics (`plugins` is always empty) |
| GET | `/api/manifest` | App name + tab manifest for the SPA |
| GET | `/api/config` | Public (non-secret) config snapshot |
| POST | `/api/chat` | Natural-language chat `{"message": "..."}` |
| GET/DELETE | `/api/chat/history` | Saved transcript (`after_id`, `limit`, `conversation_id`) |
| GET/POST | `/api/chat/conversations` | List / open saved chats |
| GET/DELETE | `/api/chat/conversations/{id}` | Switch to / delete a chat |
| POST | `/api/chat/resume` | Legacy approve/reject route (orchestrator removed) |
| GET | `/api/events` · `/api/events/stream` · `/api/events/last` | Live events + SSE feed |
| GET | `/api/tools` | Tool registry listing + stats |
| GET/POST | `/api/tasks` (+`/{id}`) | Generic task engine |
| GET/POST | `/api/memory` · GET `/api/memory/search` | Memory save/list/search (DELETE `/{id}` forgets one) |
| GET | `/api/experiences` | Experience-store stats |
| GET/POST | `/api/workflows` (+`/{id}/run`, `/runs`) | Workflow definitions + runs |
| GET/POST/PATCH/DELETE | `/api/schedules` | Scheduler CRUD |
| GET | `/api/providers` | Routable provider health |
| GET | `/api/gateway/health` | Astra AI Gateway status (10 connections + fallback) |
| GET | `/api/metrics` | Server + subsystem metrics |
| GET | `/api/v1/models` | Model registry + per-provider health |
| POST | `/api/v1/models/refresh` | Re-run model discovery |
| GET | `/api/v1/router/status` · `/api/v1/router/stats` | Routing health / stats |
| POST | `/api/v1/providers/{name}/refresh\|enable\|disable\|test\|reset-health` | Provider admin |
| POST | `/api/v1/providers/{name}/test/{model}` | Test one provider/model pair |
| POST | `/api/v1/providers/test-all` | Test every provider + Gateway connection |
| POST | `/api/v1/gateway/test` · `/api/v1/gateway/{name}/test[/{model}]` | Test the Gateway's connections |
| GET | `/api/v1/web3/transactions` (+`/{tx_id}`) | Transaction ledger |
| GET | `/api/v1/web3/transaction-policy` | Mode + limits + allowlists |
| POST | `/api/v1/web3/transaction-policy/mode` | Operator-only mode switch |
| POST | `/api/v1/web3/transactions/{tx_id}/authorize\|reject` | Operator-only approve/reject |
| GET | `/api/v1/artifacts/{id}/{filename}` · `/api/v1/uploads/{filename}` | Generated artifacts / uploaded files |

Legacy `/api/agents` + `/api/executions` routes still answer (empty list /
`410`), so old clients get a clear message instead of a 500. All JSON responses
carry `request_id`; errors are structured `{ok, error, error_code, request_id}`
and never leak stack traces (details gated to non-`production`).

### Web server (FastAPI/ASGI)

FastAPI/uvicorn is the **only** web server. `python3 run.py` starts it with the
usual banner and env handling:

```bash
pip install -r requirements.txt    # fastapi + uvicorn
python3 run.py                     # http://localhost:8787/
```

`astra/web.py` owns *what* a request means — the route table, auth, rate limit,
body cap, CORS, security headers, SSE framing and JSON envelopes — and deals in
neutral `Request`/`Response` objects. `astra/web_fastapi.py` is the only adapter
that turns those into bytes: one catch-all route, so no route can drift out of
sync. All the routes, auth, rate limits, CORS, headers, uploads, body cap and
SSE behaviour documented above are unchanged.

**Plain ASGI deployment.** The factory builds the stack on startup and stops
the scheduler + closes the store on shutdown, so process managers (systemd,
gunicorn, Docker, k8s) can own the process directly:

```bash
uvicorn --factory astra.web_fastapi:create_app --host 127.0.0.1 --port 8787
```

The app owns the lifecycle only when it was handed a `stack` (or nothing at
all); pass `site=`/`store=`/`agent=` and the caller keeps ownership, which is
how the tests run the real server in-process.

**Concurrency.** Blocking work (an agent turn, a provider probe) runs in
anyio's worker threadpool, so the event loop stays free — size it with
`ASTRA_ASGI_THREADS` (default: Starlette's 40). The SSE live feed is driven by
an async generator, so an idle Activity Log tab costs no worker thread.

* `ASTRA_FASTAPI_DOCS=1` exposes `/docs` + `/openapi.json`. Off by default —
  Swagger UI loads its JS from a CDN, which the app's own CSP blocks. The
  router is mounted as one catch-all (see `astra/web_fastapi.py`), so the
  generated schema cannot describe individual routes; the API table above is
  the authoritative reference.
* `python-multipart` is deliberately **not** required: uploads are parsed by
  the shared stdlib parser in `astra/web.py`.
* Behind a reverse proxy, run uvicorn with `--proxy-headers
  --forwarded-allow-ips=...` so rate limiting keys on the real client instead
  of the proxy.
* **Multiple workers** (`--workers N`): the scheduler daemon
  (`ASTRA_SCHEDULER=1`) would fire every schedule once per worker, and the
  per-IP rate limiter is in-memory, so the effective limit is N× the configured
  one. Events live in SQLite, so every worker streams the same activity.

## Security hardening

* **Auth** — `ASTRA_TOKEN` gates all API traffic (Bearer / `X-Astra-Token` /
  `?token=` for SSE), constant-time compare.
* **CORS** — configurable allowlist (`ASTRA_CORS_ORIGINS`); production defaults
  to same-origin only.
* **Rate limiting** — per-IP fixed window (429 on overflow).
* **Body cap** — oversized JSON bodies are rejected (413).
* **Headers** — `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  `Cross-Origin-Opener-Policy`, Content-Security-Policy.
* **Redaction** — a global generator redacts api keys/tokens/seeds/passwords/
  private keys from every outbound API response and SSE frame.
* **SSRF guard** — URL research **and browser-tool navigation** refuse
  loopback/private/link-local (and non-`http(s)`) targets unless
  `ASTRA_ALLOW_PRIVATE_URLS=1`. URL userinfo (`http://user@host/`) and
  bracketed IPv6 literals are parsed so they cannot disguise the real host.
* **Path traversal** — static file serving resolves inside `static/` only.

## Specialist agents & the plugin system

**There is no plugin loader.** The `astra.core.Plugin`/`Registry` system was
removed from the codebase; `plugins/` is an empty placeholder folder for future
third-party plugins (see `plugins/README.md`). `ACTIVE_PLUGINS` is a no-op
legacy key — read only for the public-config `plugins` count (0 by default) —
because nothing is discovered, loaded or wired from `plugins/` today. The SPA
keeps dormant plugin hooks, but the manifest reports zero plugin tabs and no
plugin script files ship.

Domain-specific work instead ships as a built-in **specialist agent**
(`astra/agents/`) — `general`, `research`, `browser`, `coding`, `files`,
`web3`, `airdrop` — registered with `AgentManager` for introspection and
routing hints. Adding a capability today means adding a specialist agent + its
tools in core, not dropping a file into `plugins/`.

### Built-in tools

**Core** (`astra/tools/builtins.py`): `remember` · `recall` · `search_memory` ·
`create_task` · `list_tasks` · `search_web` · `fetch_url` · `wallet_balances` ·
`list_files` · `read_file` · `write_file` · `search_files` · `get_health` ·
`generate_document`

**Terminal** (`astra/terminal/tools.py`, `SYSTEM_ACTION` risk):
`terminal_exec` · `terminal_start` · `terminal_status` · `terminal_stop` ·
`terminal_kill` · `terminal_history` · `terminal_sessions` · `terminal_close`

Git is done through `terminal_exec` (`git status`, `git diff`, …) — there is
deliberately no separate git tool duplicating what the shell already does.

**Web3** (`astra/web3/tools.py`): `token_balance` · `chain_status` ·
`rpc_status` · `tx_prepare` · `tx_status`

**Browser** (`astra/browser/`, registered when a browser is available):
`browser_open` · `browser_observe` · `browser_action` · `browser_extract` ·
`browser_screenshot` · `browser_close`

## Project layout

```
astra/
├── agent.py            Chat entry point (Agent.handle/resume/dashboard/…)
├── bootstrap.py         Wires the entire stack — the one source of truth
├── web_fastapi.py       ASGI adapter: turns web.py's Request/Response into
│                        real bytes; one catch-all route
├── web.py               Route table, auth, rate limit, CORS, SSE, headers
├── chat_log.py          Server-side chat transcript (survives refresh)
├── store.py             Generic SQLite storage (plain dicts, no ORM)
├── security.py          Redaction, SSRF guard, rate limiter, request_id
├── ai/                  Providers, adapters, router, Gateway, pipeline
│                        (+ capability_context.py: the ONE live capability read,
│                         gateway_contract.py: Gateway <-> Provider handoff)
├── agents/              Specialist agents + AgentManager
├── core/                Config, events, permissions, tasks, state, …
├── tools/               ToolRegistry + built-in tools
├── terminal/            Shared persistent Terminal (sessions, manager, tools)
├── web3/                Wallet / transaction subsystem (+ raw_tx codec)
├── browser/             Optional Playwright-backed browsing
├── memory/              Layered memory + learned experience
├── research/            stdlib URL/metadata lookup helper
└── workflows/           Step workflow engine + scheduler
plugins/                  Empty placeholder (no loader)
static/                   SPA frontend (vanilla JS + CSS)
tests/                    unittest + pytest suite
```

## Development

```bash
python3 -m unittest discover -s tests -v            # full suite, stdlib runner
python3 -m pytest -q tests/test_fastapi_server.py   # live smoke: boots the real
                                                    # FastAPI server and checks the API
python3 -m compileall astra                         # syntax sanity
```

For a manual end-to-end check, start a server and hit it:
`NO_BROWSER=1 python3 run.py` then `curl -s localhost:8787/api/health`.

Optional dev extras: `pip install -r requirements-dev.txt` (pytest, Playwright).

Notable tests: `test_zero_bypass_hardening.py` (architectural regression guard:
one AI path, Gateway never a fallback provider, disjoint credentials),
`test_gateway_*.py` (Gateway routing/recovery/supervision/task completion),
`test_web3*.py` (policy engine + tool wiring), `test_fastapi_server.py` (real
server over HTTP), `test_security.py` / `test_per_key_health.py` /
`test_provider_error_isolation.py` (redaction, credential health, isolation).

Docker: `docker build -t astra-agent . && docker run -p 8787:8787 astra-agent`.
