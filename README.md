# Astra AI Agent 🚀

A **plugin-based local Personal AI OS** — a zero-dependency core that plans,
routes, remembers and executes goals, plus the **Airdrop Manager** as its
first plugin. Python 3.9+ standard library only. No `pip install` to run.

Astra is a **Personal AI OS**: a generic core (orchestrator, planner, tool
registry, memory, workflows, scheduler) with domain-specific features living
in **plugins**. Want token tracking, a calendar, trading alerts, notes? Write
one plugin, drop it in `plugins/`, restart. The core never changes.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Astra AI Agent — Personal AI OS                                        │
│                                                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ Orchestrator │  │  Planner    │  │   Memory     │  │  Workflows    │  │
│  │ (exec loop)  │  │ (NL→steps)  │  │ (layered)    │  │  (DAG engine) │  │
│  └─────────────┘  └─────────────┘  └──────────────┘  └───────────────┘  │
│                                                                          │
│  ┌──────────────────────┐   ┌─────────────────────────────────────────┐ │
│  │   AgentRouter        │   │  Provider Adapters (10)                 │ │
│  │   CENTRAL routing    │   │  gemini groq mistral openrouter         │ │
│  │   core — it is NOT   │──▶│  cerebras cloudflare sambanova cohere   │ │
│  │   a provider         │   │  zai bedrock (+claude / openai-compat)  │ │
│  └──────────────────────┘   └─────────────────────────────────────────┘ │
│                                                                          │
│  ┌──────────────────────┐   ┌─────────────────────────────────────────┐ │
│  │ Web3 Transaction     │   │ Security layer                          │ │
│  │ Manager + deterministic   │ auth · rate-limit · CORS · request_id  │ │
│  │ policy (CONFIRM/AUTO)│   │ secret redaction · SSRF guard · body cap│ │
│  └──────────────────────┘   └─────────────────────────────────────────┘ │
│                                                                          │
│  Store: SQLite · server: stdlib HTTP + SSE · UI: SPA                   │
└──────────────────────────────────────────────────────────────────────────┘
```

### Key subsystems

| Subsystem | What it does |
|-----------|-------------|
| **AgentRouter** | **The central AI routing core.** Scores task types, ranks provider/model candidates by health+score, rotates credentials, learns from outcomes, and records routing stats. It consumes the 10 adapters + model registry — it is **never** registered as a provider itself. It optionally holds one gateway of its own: the **Astra AI Gateway** (`GW_*` config in `astra/ai/gateway.py`), four independent AI connections with automatic fallback (Gemini → Groq → Cloudflare → Bedrock), used only as a last-resort fallback after every real provider has failed, and reported separately from the provider table. |
| **Provider adapters (10)** | Gemini, Groq, Mistral, OpenRouter, Cerebras, Cloudflare, SambaNova, Cohere, Z.ai, Bedrock — one shared OpenAI-compatible adapter class + a real AWS SigV4 Bedrock adapter. Unlimited credentials per provider via key pools. |
| **Model registry** | Capabilities, context window, streaming/tools/vision support, cost/speed/quality classes, preferred/disabled status. |
| `Orchestrator` | UNDERSTAND → PLAN → SELECT TOOL → EXECUTE → OBSERVE → VERIFY → LEARN → CONTINUE. Recoverable across restarts (WAITING_USER runs are reconstituted, never blanket-failed). |
| `Planner` | NL goal → ordered, dependency-aware tool steps. |
| `ToolRegistry` | Builtin + plugin tools with schema validation, permission policy, timeout/retry/rate-limit, audit trail. |
| `MemorySystem` | Layered memory (working/short/long/semantic/episodic) with importance scoring + search. |
| `ExperienceStore` | Learns from past successes/failures. |
| `WorkflowEngine` + `SchedulerManager` | Multi-step workflows, run on demand or cron-like schedules. |
| `EventBus` | Persisted events + SSE streaming to the Live tab. |
| `Web3 Manager` | Lifecycle-tracked transactions (CREATED→…→CONFIRMED/FAILED), deterministic CONFIRM/AUTO policy, never-sign-twice, on-chain recovery. Private keys never leave the secure keystore. |

---

## Quick Start

```bash
git clone https://github.com/mainnetwallet/Astra-AI-Agent.git
cd Astra-AI-Agent
bash setup.sh
bash start.sh
# Open http://localhost:8787/
```

That's it. No `pip install`. No database setup. Python 3.9+ only.

Windows: double-click `setup.bat` then `start.bat` (or `setup.ps1`/`start.ps1`).
Termux/Android, macOS: same `setup.sh`/`start.sh` flow.

> ⚠️ **Security default:** the server binds **127.0.0.1** (loopback). Add
> `ASTRA_TOKEN=...` to your `.env` (or env) before opening it to your LAN with
> `BIND=0.0.0.0` — every `/api/*` call then requires the token.

---

## Setting up AI providers

Copy `.env.example` to `.env` and fill in at least one provider key list.
Each value is a space/comma-separated list — unlimited credentials, rotated
per request by the router:

| Provider | Env (keys) | Env (models, optional) |
|----------|-----------|------------------------|
| Gemini | `GEMINI_API_KEYS` | `GEMINI_MODELS` |
| Groq | `GROQ_API_KEYS` | `GROQ_MODELS` |
| Mistral | `MISTRAL_API_KEYS` | `MISTRAL_MODELS` |
| OpenRouter | `OPENROUTER_API_KEYS` | `OPENROUTER_MODELS` |
| Cerebras | `CEREBRAS_API_KEYS` | `CEREBRAS_MODELS` |
| Cloudflare | `CLOUDFLARE_API_KEYS` (+`CLOUDFLARE_ACCOUNT_IDS`) | `CLOUDFLARE_MODELS` |
| SambaNova | `SAMBA_API_KEYS` | `SAMBA_MODELS` |
| Cohere | `COHERE_API_KEYS` | `COHERE_MODELS` |
| Z.ai | `ZAI_API_KEYS` | `ZAI_MODELS` |
| Bedrock | `BEDROCK_CREDENTIALS` (`access_key:secret_key:region`) | `BEDROCK_MODELS` |

Single-provider chat also works via `ANTHROPIC_API_KEY` (Claude) or
`AI_BASE_URL`/`AI_MODEL`/`AI_API_KEY` (any OpenAI-compatible endpoint —
OpenRouter, Ollama, LM Studio…). All keys stay in env/`.env` — never in the DB,
logs, UI, or API responses. Router preference order: `AI_PROVIDER=gemini groq …`.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 8787 | HTTP server port |
| `BIND` | 127.0.0.1 | Bind address (secure local default) |
| `ASTRA_TOKEN` | *(none)* | Operator token — protects every `/api/*`+`/api/v1/*` |
| `ENV` | development | `production` hides error details |
| `ASTRA_API_RATE_LIMIT` | 300 | Per-IP requests/minute |
| `ASTRA_MAX_BODY_MB` | 10 | Max request body size |
| `ASTRA_CORS_ORIGINS` | *(reflect)* | Allowlisted origins (space-separated) |
| `ASTRA_ALLOW_PRIVATE_URLS` | 0 | SSRF guard override for research |
| `WEB3_TRANSACTION_MODE` | CONFIRM | `CONFIRM` (manual) or `AUTO` (policy-only) |
| `WEB3_MAX_TX_VALUE_WEI` / `WEB3_MAX_DAILY_TX_VALUE_WEI` / `WEB3_MAX_GAS_LIMIT` | — | Deterministic policy limits |
| `WEB3_ALLOWED_RECIPIENTS` / `WEB3_ALLOWED_CONTRACTS` / `WEB3_ALLOWED_WALLETS` | — | Space-separated allowlists |
| `WEB3_CHAIN_IDS` | 1 8453 137 56 42161 10 11155111 | Chain whitelist |
| `DATA_DIR` | ./data | SQLite + config storage |
| `NO_BROWSER` | false | Don't auto-open browser on start |
| `ACTIVE_PLUGINS` | airdrop | Comma-separated plugin whitelist |
| `ASTRA_SCHEDULER` | 0 | Start the scheduler daemon |

## Web3 transaction safety

* **CONFIRM** (default): every transaction stops at `WAITING_USER` for your
  review — the LLM can only *prepare* a send, never authorise or sign. The
  operator approves via `POST .../transactions/{tx_id}/authorize` (signs +
  broadcasts) or rejects via `.../reject` — both require `ASTRA_TOKEN`.
* **AUTO**: only transactions that pass the *deterministic* policy
  (sender/recipient/contract allowlist, max per-tx, daily budget, gas, chain)
  send automatically — no per-transaction confirmation — for user-authorized
  wallets. A tx outside AUTO policy is never auto-approved. The LLM can
  **never** change mode, policy, or add wallets — only the operator (with
  `ASTRA_TOKEN`) can.
* Emergency stop halts all sends. A `WAITING_USER` tx is never silently
  approved on a mode switch.
* Never-sign-twice is enforced cryptographically (deterministic tx hash + MAC)
  and an uncertain tx is resolved by querying the chain by hash — never blindly
  re-submitted. Private keys never appear in logs, events, API responses or
  plaintext DB rows — signing uses the secure keystore only.

## Web UI tabs

| Tab | What it shows |
|-----|-------------|
| **Dashboard** | Overview cards — plug to your plugin data |
| **Assistant** | Chat interface — natural language commands |
| **Live** | Real-time SSE event stream, health, tools, executions |
| **Providers** | AI provider health, latency, calls/errors, model refresh |
| **Router** | Model registry + task routing stats (the AgentRouter's view) |
| **Wallet** | Web3 transaction policy (mode, limits, allowlists) + recent txs |
| **Backup** | Export/import all data as one JSON file |

## API (versioned)

Every route works under `/api/...` (legacy) **and** `/api/v1/...` (same
handler). New endpoints below are documented under `/api/v1`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/health` | DB / plugins / providers / scheduler checks |
| GET | `/api/v1/manifest` | Plugin + tab manifest for the SPA |
| POST | `/api/v1/chat` | Natural-language chat `{"message": "..."}` |
| GET | `/api/v1/models` | Model registry + per-provider health |
| POST | `/api/v1/models/refresh` | Re-run model discovery |
| GET | `/api/v1/router/status` · `/api/v1/router/stats` | Routing health / stats |
| POST | `/api/v1/providers/&lt;name&gt;/refresh\|enable\|disable` | Provider admin |
| GET | `/api/v1/web3/transactions` (+`/{tx_id}`) | Transaction ledger |
| GET | `/api/v1/web3/transaction-policy` | Mode + limits + allowlists + stop |
| POST | `/api/v1/web3/transaction-policy/mode` | Operator-only mode switch |
| POST | `/api/v1/web3/transactions/{tx_id}/authorize` | Operator-only: approve + sign + broadcast a `CONFIRM`-mode send |
| POST | `/api/v1/web3/transactions/{tx_id}/reject` | Operator-only: reject a pending send |
| GET | `/api/metrics` | Server + subsystem metrics (requests, router, tools, web3) |
| GET | `/api/v1/tasks` · `/api/v1/memory` · `/api/v1/workflows` · `/api/v1/schedules` | Task/memory/workflow/schedule APIs |
| GET | `/api/v1/events/stream` | SSE live feed |

All JSON responses carry `request_id`; errors are structured
`{ok, error, error_code, request_id}` and never leak stack traces (details
gated to non-`production`). Every response is secret-redacted, secured with
CSP/nosniff/frame headers and optionally rate-limited + body-capped.

## Security hardening

* **Auth** — `ASTRA_TOKEN` gates all API traffic (Bearer / `X-Astra-Token` /
  `?token=` for SSE), constant-time compare.
* **CORS** — configurable allowlist (`ASTRA_CORS_ORIGINS`); production defaults
  to same-origin only.
* **Rate limiting** — per-IP fixed window (429 on overflow).
* **Body cap** — oversized JSON bodies are rejected (413).
* **Headers** — `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, `Cross-Origin-Opener-Policy`, Content-Security-Policy.
* **Redaction** — a global generator redacts api keys/tokens/seeds/passwords/
  private keys from every outbound API response and SSE frame, so a leaky
  subsystem can't egress a secret.
* **SSRF guard** — URL research refuses loopback/private/link-local targets
  unless `ASTRA_ALLOW_PRIVATE_URLS=1`.
* **Path traversal** — static file serving resolves inside `static/` only.

## Plugin system

One plugin ships: **Airdrop Manager** (`plugins/airdrop/`).

### Built-in tools

`search_web` · `remember` · `recall` · `get_health` · `create_task` ·
`list_tasks` · `read_file` · `write_file` · `search_files` ·
`wallet_balances` · `fetch_url` · `answer` · `wallet_validate` · `browse` …

### Adding a new plugin

Create `plugins/myplugin/__init__.py` with a class inheriting `Plugin`:

```python
from astra.core.plugins import Plugin

class MyPlugin(Plugin):
    slug = "myplugin"
    title = "My Plugin"
    version = "0.1.0"

    def startup(self): ...
    def shutdown(self): ...
    def tools(self):
        return [{"name": "my_tool", "fn": self.my_tool}]
```

Drop it in `plugins/`, add `"myplugin"` to `ACTIVE_PLUGINS`, restart.

## Development

```bash
python3 -m unittest discover -s tests -v   # 213+ unit tests, stdlib runner
python3 smoke_test.py                      # end-to-end smoke suite
python3 -m compileall astra                # syntax sanity
```

Optional dev extras: `pip install -r requirements-dev.txt` (pytest, Playwright).
Docker: `docker build -t astra-agent . && docker run -p 8787:8787 astra-agent`.