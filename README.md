# Astra AI Agent 🚀

A **general-purpose, plugin-based local AI assistant** — with the **Airdrop
Manager** as its first plugin.

The design is deliberately *not* airdrop-only: the core (`astra/`) is a
generic agent shell (storage, chat router, web server, UI) and every feature
lives in a **plugin**. Want a future feature — token price tracking, a
calendar, notes, trading alerts — you (or I) write *one new plugin file* and
drop it in. The core never changes.

Everything runs on your own machine. Zero cloud, zero `pip install`, zero
dependencies beyond Python 3's standard library. Works on Android/Termux.

```
┌─────────────────────────────────────────────┐
│  Astra AI Agent (core)                     │
│  ┌─────────┐ ┌──────┐ ┌──────┐ ┌─────────┐ │
│  │ Store   │ │Agent │ │ Web  │ │  UI     │ │
│  │ (SQLite)│ │(chat)│ │(HTTP)│ │(SPA)    │ │
│  └─────────┘ └──────┘ └──────┘ └─────────┘ │
│          ▲           plugin registry        │
│  ┌───────────────────┐                       │
│  │  Plugin: Airdrops │  ← the first plugin   │
│  └───────────────────┘                       │
│  (future plugins plug in here)               │
└─────────────────────────────────────────────┘
```

---

## 1. What Astra does today (with the Airdrops plugin)

| Tab | What it does |
|---|---|
| 📊 Dashboard | One shared dashboard; the Airdrops plugin contributes KPI cards (total/active, 7d deadlines, pending tasks, wallets) + upcoming & overdue deadline lists |
| 📦 Airdrops | Full CRUD — name, project, status dropdown (new/active/farming/claimable/done/dropped), deadline, network, reward, est. value, link, phase; per-airdrop task strip |
| ✅ Tasks | Add social/onchain/wallet tasks per airdrop, tick done / untick |
| 👛 Wallets | Add labelled addresses (EVM `0x…`, TON `0:…` / `UQ…`, TRON `41…`, SOL base58) with a live format check |
| 🤖 Assistant | Natural-language chat in Banglish or English (`add airdrop Hamster deadline 30 oct reward token`, `deadlines this week`, `progress`, `add wallet 0x…`) |
| 💾 Backup | Export everything (all plugins) to one JSON file; import it back later, dedupe-safe |

### Chat example

```
you:  add airdrop Notcoin deadline 30 oct reward points value 500 network TON
astra: ✅ Airdrop 'Notcoin' added (id #2).
       • Network: TON
       • Deadline: 2026-10-30 (in Xd)
       • Est. value: 500
you:  add task "join telegram" to Notcoin
you:  deadlines this week
```

Dates understand ISO (`2026-12-31`), `DD/MM/YYYY`, `31 dec`, `tomorrow`,
`next week`, and Banglish (`kal`, `agami kal`).

When `ANTHROPIC_API_KEY` is set, anything no plugin understands goes to a real
Claude answer. Every structured command runs 100% locally, offline.

---

## 3. Technologies used

| Layer | Tech | Why |
|---|---|---|
| Language | **Python 3.9+** stdlib only | Runs anywhere Python exists — Termux, servers, laptops. No pip. |
| Storage | `sqlite3` | Zero-config, single file, ACID, in stdlib. Plugins each get their own tables. |
| HTTP | `http.server.ThreadingHTTPServer` | No Flask/uvicorn; threaded so plugin work never blocks the UI. |
| Agent brain | Rule-based NL parser (`re`) inside plugins | Instant, deterministic, offline; each plugin owns its domain's grammar. |
| AI upgrade | Anthropic Messages API via `urllib` | Optional. Only free-form chat leaves the machine (when a key is set). |
| Frontend | Vanilla HTML/CSS/JS — **manifest-driven** | `GET /api/manifest` lists tabs; plugins register their own JS + dashboard blocks. No bundler. |
| Tests | `unittest` | 49 tests + 30 live HTTP smoke checks, all green. |

**Zero external dependencies.** No `requirements.txt`, no `pip install`.

---

## 4. How to install

- Python **3.9+** (tested through 3.14) and a web browser. That's it.

```bash
# put the folder anywhere — copy or unpack
cd astra-agent
# nothing to install; the app is ready
```

On Android/Termux the same folder works under a PROot/Linux distro (`python3 run.py`).

---

## 5. How to start

```bash
python3 run.py
```

The dashboard opens at `http://localhost:8787/`. Ctrl+C stops it.

### Environment variables (all optional)

| Env var | Default | What it does |
|---|---|---|
| `PORT` | `8787` | Listen port |
| `BIND` | `0.0.0.0` | Set `BIND=127.0.0.1` for local-only access |
| `DATA_DIR` | `./data` | Where `astra.db` (SQLite) is stored |
| `ANTHROPIC_API_KEY` | *(none)* | Enables AI Q&A chat |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` | Model for free-form Q&A |
| `NO_BROWSER` | `1` | Set `1` to not auto-open a browser |

Example:
```bash
PORT=9000 BIND=127.0.0.1 DATA_DIR=/sdcard/astra python3 run.py
```

### Adding a future feature (the point of the plugin system)

1. Create `plugins/<name>.py` that subclasses `astra.core.Plugin`
   (`slug`, `title`, `icon`, `SCHEMA`, `process()`, `routes()`, `summary()`,
   `export()`, `import_data()`).
2. End it with `Plugin = YourPluginClass`.
3. Add `"plugins.<name>"` to `PLUGIN_MODULES` in `run.py`.

No other file changes. The tab, dashboard block, chat intents and API routes
all appear automatically.

---

## 6. Environment variables required

**None.** It works with zero configuration out of the box.

---

## 7. Test results

```
$ python3 -m unittest discover -s tests
.........................
----------------------------------------------------------------------
Ran 49 tests in 0.611s

OK
```

| Module | Tests | What is covered |
|---|---|---|
| `tests/test_store.py` | 6 | Generic SQLite store: insert/fetch, param querying (injection-safe), lastrowid, idempotent schema install |
| `tests/test_airdrop_plugin.py` | 33 | `parse_date` (ISO/DD-MM/words/Banglish), all storage accessors, deadlines, task lifecycle, wallet validate edge cases, export/import roundtrip, NL chat (add/delete airdrop, tasks, wallets, progress, help, offline fallback) |
| `tests/test_web.py` | 10 | Manifest (name = "Astra AI Agent", plugins + tabs), static serving, full airdrop CRUD over HTTP, task flow + aggregated dashboard, wallet validate/add/reject, chat via API, export/import, 404 |

Live end-to-end smoke test (real HTTP, boots exactly like `run.py`):

```
$ python3 smoke_test.py
  ✅  index.html served (Astra shell)      ✅  manifest 200 + name
  ✅  airdrop plugin js served             ✅  dashboard empty airdrops card
  ✅  create airdrop 201                   ✅  import dedupes
  ... all 30 checks ...
========================================
  ALL CHECKS PASSED ✅
========================================
```

---

## 8. Known limitations

1. **No OS push notifications** for deadlines — they're visible on the
   dashboard; a cron/Tasker job can hit `GET /api/dashboard` to alert you.
2. **No login/password** — the server binds `0.0.0.0` by default. For privacy
   use `BIND=127.0.0.1` or a firewall (see Security).
3. **Single user** — no accounts or multi-user support.
4. **No blockchain lookups** — wallets are labelled references; balances are
   not fetched.
5. **Research helper is an optional, offline-graceful URL title/meta fetch** —
   not a chain verifier and not a full project audit.
6. **Date parsing is English/Banglish-centric** — Bangla calendar (১৪XX) isn't
   handled.
7. **No automated claiming** — Astra tracks and organises work; on-chain
   actions are never automated.

---

## 9. Security considerations

1. **Local-first** — all data stays in `data/astra.db`. Nothing is transmitted
   unless you export the JSON yourself.
2. **Binding** — default `0.0.0.0` exposes the UI to your LAN. Use
   `BIND=127.0.0.1` for local-only.
3. **Never store keys** — only public addresses are saved. A private key or
   seed phrase is *always* a scam request; Astra will never ask for one.
4. **External calls only for free-form chat** — and only when you set
   `ANTHROPIC_API_KEY`. Structured commands never leave the machine.
5. **SQL-injection safe** — every query uses bound parameters; the store has
   no string-interpolation helpers.
6. **XSS-safe UI** — all rendered text goes through `esc()` in `astra.js`;
   plugin JS files shipped with the app follow the same rule.
7. **One plugin can't break the chat** — `astra/agent.py` wraps every
   plugin's `process()` in a try/except, so a failing plugin is skipped, not
   fatal.

---

## 10. Next recommended development steps

1. **Deadline push reminders** — background thread → `notify-send` (desktop)
   or a Telegram bot message when a deadline crosses `today + N`.
2. **Telegram bot plugin** — a `plugins/telegram.py` exposing the same chat
   agent over a Telegram bot token.
3. **Auth plugin** — a small `plugins/auth.py` adding browser basic-auth to
   the dashboard.
4. **Balance checker plugin** — call public RPCs (`eth_getBalance`, TON HTTP
   API) and show balances beside each wallet.
5. **Profitability tracker plugin** — estimated vs actual claim value, monthly
   P&L summary.
6. **New-domain plugins** to prove the platform: Notes, airdrop calendar,
   referral/community tracking.
7. **PWA** — service worker + installable home-screen entry for phones.
8. **CSV/Spreadsheet export** for people who prefer Excel.
9. **Deeper LLM tool use** — give the agent a web-search tool so free-form
   questions can research projects and report in chat.
10. **Backup to cloud** — optional encrypted export to a service you choose.

---

*Built by Astra AI Agent — local-first, plugin-based, zero-dependency,
privacy-respecting. 🚀*