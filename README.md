# Astra AI Agent 🚀

A **general-purpose, plugin-based local AI assistant** built for personal
automation — with the **Airdrop Manager** as its first plugin.

Astra is a **Personal AI OS**: a generic core (orchestrator, planner, tool
registry, memory, workflows, scheduler) with domain-specific features living
in **plugins**. Want token tracking, a calendar, trading alerts, notes? Write
one plugin file, drop it in, and restart. The core never changes.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Astra AI Agent — Personal OS                                       │
│                                                                      │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────────┐ │
│  │ Orchestrator│  │  Planner   │  │   Memory   │  │   Workflows    │ │
│  │ (exec loop) │  │ (NL→steps) │  │ (long+short│  │  (DAG engine)  │ │
│  └────────────┘  └────────────┘  │  term)     │  └────────────────┘ │
│                                   └────────────┘                     │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────────┐ │
│  │Tool Registry│  │   Agent    │  │   Config   │  │   Scheduler    │ │
│  │  + policy   │  │  (brain)   │  │(.env/json) │  │  (cron-like)   │ │
│  └────────────┘  └────────────┘  └────────────┘  └────────────────┘ │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  Plugin System 2.0                                            │  │
│  │  plugins/airdrop/  — AirdropManager (tasks, wallets, deadlines)│  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  Web UI (SPA) — Dashboard / Assistant / Live / Airdrop tabs    │  │
│  │  HTTP API — REST + SSE streaming                               │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  Store: SQLite (zero deps, single file)                              │
│  AI: Anthropic Claude (HTTP) or Offline mode                         │
└──────────────────────────────────────────────────────────────────────┘
```

### Key subsystems

| Subsystem | What it does |
|-----------|-------------|
| `Orchestrator` | UNDERSTAND → PLAN → SELECT TOOL → EXECUTE → OBSERVE → VERIFY → LEARN → CONTINUE |
| `Planner` | NL goal → ordered tool steps (offline regex first, LLM fallback) |
| `ToolRegistry` | 13+ builtin tools, policy gating, audit trail |
| `MemorySystem` | Short-term (deque) + long-term (keyword-scored SQLite) recall |
| `ExperienceStore` | Pattern library: learn from past successes/failures |
| `WorkflowEngine` | Define multi-step workflows, run on demand or via scheduler |
| `SchedulerManager` | daily/weekly/interval/oneshot/deadline — no external cron needed |
| `EventBus` | Persisted events + SSE streaming to the Live tab |
| `TaskEngine` | Generic DAG tasks with priorities, dependencies, status tracking |
| `Config` | env var → config.json → .env → default (zero setup needed) |

---

## Quick Start

### One-line setup

```bash
# Linux / Termux
bash <(curl -s https://raw.githubusercontent.com/.../setup.sh)

# Or clone and run manually
git clone https://github.com/mainnetwallet/Astra-AI-Agent.git
cd Astra-AI-Agent
bash setup.sh
```

### Start the agent

```bash
bash start.sh
# Open http://localhost:8787 in your browser
```

That's it. No `pip install`. No database. Python 3.9+ only.

---

## Platform Commands

### Linux (Debian/Ubuntu)

```bash
# Install Python (if missing)
sudo apt update && sudo apt install -y python3

# Setup + Start
cd Astra-AI-Agent
bash setup.sh
bash start.sh
```

### Termux (Android)

```bash
# Install Python
pkg update && pkg install -y python

# Setup + Start
cd Astra-AI-Agent
bash setup.sh
bash start.sh
# Open http://localhost:8787 in your phone browser
```

### Windows (PowerShell)

```powershell
# Open PowerShell, navigate to project folder
cd Astra-AI-Agent

# Setup (creates config, checks Python)
.\setup.ps1

# Start
.\start.ps1
# Open http://localhost:8787 in your browser
```

### Windows (double-click)

1. Double-click `setup.bat` first (one-time setup)
2. Double-click `start.bat` to launch
3. Open http://localhost:8787

### macOS

```bash
brew install python3
cd Astra-AI-Agent
bash setup.sh
bash start.sh
```

---

## Environment Variables

All prefixed with `ASTRA_`. Set in `.env` file, `config.json`, or shell env.

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 8787 | HTTP server port |
| `HOST` | 0.0.0.0 | Bind address |
| `ANTHROPIC_API_KEY` | *(none)* | Claude API key — leave empty for offline mode |
| `AI_PROVIDER` | anthropic | `anthropic` or `offline` |
| `AI_MODEL` | *(auto)* | Override model (e.g. `claude-sonnet-4-20250514`) |
| `LOG_LEVEL` | info | `debug`, `info`, `warning`, `error` |
| `DATA_DIR` | ./data | SQLite + config storage path |
| `NO_BROWSER` | false | Don't auto-open browser on start |
| `ACTIVE_PLUGINS` | airdrop | Comma-separated plugin slugs |

---

## Web UI Tabs

| Tab | What it shows |
|-----|-------------|
| **Dashboard** | Overview cards — airdrops, tasks, wallets, deadlines |
| **Assistant** | Chat interface — natural language commands |
| **Live** | Real-time SSE event stream, system health, tool activity |
| **Airdrop** | Create/edit/delete airdrops, manage tasks & wallets |

---

## API Endpoints (selected)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | System health (DB, plugins, AI, scheduler) |
| GET | `/api/manifest` | Plugin + tab manifest for SPA |
| GET | `/api/tools` | All registered tools |
| POST | `/api/agents` | Submit a goal (`{"goal": "...", "sync": true}`) |
| GET | `/api/executions` | Execution history |
| POST | `/api/tasks` | Create a task |
| POST | `/api/memory` | Save to memory |
| GET | `/api/memory/search?query=...` | Search memory |
| POST | `/api/workflows` | Define a workflow |
| POST | `/api/schedules` | Create a schedule |
| GET | `/api/events/stream` | SSE event stream |
| POST | `/api/chat` | Natural language chat |
| GET | `/api/dashboard` | Dashboard cards |

---

## Plugin System

Astra ships with one plugin: **Airdrop Manager** (`plugins/airdrop/`).

### Built-in tools (13)

`search_web` · `remember` · `recall` · `get_health` · `create_task` · `list_tasks` · `read_file` · `write_file` · `search_files` · `wallet_balances` · `fetch_url` · `answer` · `wallet_validate`

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

---

## Testing

```bash
# Unit tests (102 tests)
python3 -m unittest discover -s tests -q

# Smoke test (44 checks — full HTTP integration)
python3 smoke_test.py
```

All tests use in-memory SQLite — no side effects, no cleanup needed.

---

## Database

Single SQLite file at `DATA_DIR/astra.db`. Tables auto-created on first boot:

- `events` — audit trail + Live stream
- `astra_tasks` — generic task engine (DAG)
- `schedules` — scheduler definitions
- `astra_sched_seen` — deadline dedup
- Airdrop plugin tables: `airdrops`, `airdrop_tasks`, `wallets`

No migrations needed — `user_version` tracked, schema evolves forward.

---

## Known Limitations

1. **Offline mode**: Without `ANTHROPIC_API_KEY`, the agent uses regex intent
   matching only — no AI reasoning for complex multi-step goals.
2. **No browser automation**: Browser tools are stubs (no Playwright/Selenium
   dependency). Research via `fetch_url` (HTTP fetch) only.
3. **Single-user**: No authentication. Only expose on localhost or trusted network.
4. **No persistence of orchestrator state**: In-flight async executions are
   in-memory only; restart loses running state.
5. **Workflow scheduler runs in-process**: No external cron daemon. If the
   process dies, scheduled runs are missed until restart.
6. **No real wallet balance queries**: `wallet_balances` returns placeholder
   data until a Web3 provider (Alchemy/Infura) is configured in a plugin.
7. **Mobile UI**: Functional but not pixel-perfect on all screen sizes.

---

## Project Structure

```
astra-agent/
├── astra/                  # Generic core (never contains domain logic)
│   ├── core/               # orchestrator, planner, tasks, events, config...
│   ├── ai/                 # provider abstraction, agent router
│   ├── tools/              # tool registry, schemas, builtins
│   ├── memory/             # memory + experience store
│   ├── web.py              # HTTP server + API + SSE
│   └── bootstrap.py        # wires everything together
├── plugins/
│   └── airdrop/            # AirdropManager plugin (first domain feature)
├── static/                 # SPA frontend (HTML/CSS/JS, zero build step)
├── tests/                  # 102 unit tests
├── smoke_test.py           # 44-check integration test
├── run.py                  # Entry point
├── setup.sh / .ps1 / .bat  # Platform setup scripts
├── start.sh / .ps1 / .bat  # Platform start scripts
└── config.json             # (auto-created, gitignored)
```

---

## License

MIT
