# Astra AI Agent — Windows Installation Guide

This guide installs and runs Astra AI Agent on **Windows 10** and **Windows 11**. It covers the web application, the AI provider configuration, and the additional setup required for the isolated **Agent Runtime** (WSL2 + Ubuntu).

Every command in this guide was checked against the repository source (`setup.bat`, `setup.ps1`, `start.bat`, `start.ps1`, `run.py`, `requirements.txt`, `pyproject.toml`, `.env.example`, `astra/runtime/`). Commands marked *(Windows tool)* come from Windows itself, not from Astra.

---

## 1. Introduction

Astra AI Agent is a local Personal AI OS: a FastAPI/uvicorn web application with a multi-provider AI router, a web UI, and a tool registry. Alongside it runs the **Agent Runtime** — an isolated Linux environment (WSL2 + Ubuntu on Windows) in which all agent-issued commands execute.

**What this guide covers**

- Installing Astra and its dependencies on Windows 10/11
- Creating and editing the `.env` configuration file
- Starting Astra, opening the web interface, and stopping it safely
- Installing and verifying the WSL2 + Ubuntu Agent Runtime
- Troubleshooting, updating, and the verified command reference

**Supported Windows versions:** Windows 10 and Windows 11. The Agent Runtime additionally requires WSL2 with an Ubuntu distribution, which is a separate, manual Windows feature you enable yourself.

For the full architecture, the API surface, and the runtime internals, see [README.md](README.md), [ARCHITECTURE.md](ARCHITECTURE.md), and [docs/AGENT_RUNTIME.md](docs/AGENT_RUNTIME.md).

---

## 2. Prerequisites

### 2.1 Python — required

Astra requires **Python 3.9 or later** (`pyproject.toml` sets `requires-python = ">=3.9"`; the `Dockerfile` builds on Python 3.11).

1. Download the installer from <https://www.python.org/downloads/windows/>.
2. During installation, **tick "Add python.exe to PATH"** on the first screen. Both `setup.bat` and `setup.ps1` check for `python` on `PATH` and stop with an error if it is missing.
3. Verify (PowerShell or Command Prompt):

```powershell
python --version
```

### 2.2 Git — required for the clone step

```powershell
git --version
```

Install from <https://git-scm.com/download/win> if that command is not recognised.

### 2.3 Web browser

Astra serves a web interface on localhost; you need a current browser to use it.

### 2.4 WSL2 + Ubuntu — required only for the Agent Runtime

The Agent Runtime is what runs the agent's shell commands, file operations, and package installs. On Windows it is **WSL2 + Ubuntu**, and **Astra never installs it for you**. You install it once, yourself.

Run in an **elevated** PowerShell (right-click PowerShell → *Run as administrator*):

```powershell
wsl --install -d Ubuntu
```

Then complete the first-launch setup of Ubuntu (it asks you to create a Linux username and password), and confirm the distribution is on WSL2:

```powershell
wsl --list --verbose
```

The `VERSION` column must read `2`. If it reads `1`, convert it:

```powershell
wsl --set-version Ubuntu 2
```

> **This step is separate from Astra.** Astra reports a missing distribution as *"Agent Runtime unavailable: WSL2 Ubuntu is not installed"* and fails closed — it never silently substitutes Command Prompt or PowerShell. You can use the web application without it; only agent-executed work needs it.

---

## 3. Download and Install

### 3.1 Clone the repository

From the folder where you want the project to live:

```powershell
git clone https://github.com/mainnetwallet/Astra-AI-Agent.git
cd Astra-AI-Agent
```

**ZIP alternative:** Go to <https://github.com/mainnetwallet/Astra-AI-Agent>, choose **Code → Download ZIP**, extract it, and open PowerShell inside the extracted folder. Note that with a ZIP download you have no Git history, so the update flow in Section 9 (which uses `git pull`) does not apply.

### 3.2 Run the setup script

The repository ships two setup scripts that do the same work. Both verify Python, install dependencies, create `data/`, and create `config.json` if it does not exist.

**Option A — Command Prompt (double-click friendly):**

```cmd
setup.bat
```

**Option B — PowerShell (recommended; clearer output, and it also tells you whether a `.env` already exists):**

```powershell
.\setup.ps1
```

Both run the same underlying command, which you can also run yourself:

```powershell
python -m pip install --upgrade -r requirements.txt
```

`requirements.txt` installs only `fastapi>=0.110` and `uvicorn>=0.27`. Everything else the agent uses is the Python 3.9+ standard library, so there is nothing else to install for a working server.

### 3.3 What setup creates

| Path | Purpose | Created by |
| --- | --- | --- |
| `data/` | Local storage (SQLite database, screenshots) | `setup.bat`, `setup.ps1` |
| `config.json` | Optional server settings; git-ignored | `setup.bat`, `setup.ps1`, if absent |

Both scripts leave an existing `config.json` untouched, so local port/bind settings survive a re-run.

---

## 4. Configure AI Providers

Astra needs credentials for **at least one AI provider** before chat will answer. Without a key, the server still starts, but the chat pipeline reports that AI is "not configured" in its startup banner.

### 4.1 Create `.env` from the template

The repository ships `.env.example` as a commented template. Copy it, then edit the copy:

```powershell
Copy-Item .env.example .env
```

(Command Prompt equivalent: `copy .env.example .env`.)

> **Recommendation:** open the new file in Notepad rather than appending lines from the shell:
>
> ```powershell
> notepad .env
> ```
>
> Appending with `Add-Content` / `echo >>` can create duplicate entries for the same variable and can introduce encoding problems on a non-UTF-8 console. Editing the copied file keeps exactly one entry per setting.

### 4.2 Fill in a provider key

`.env.example` groups keys into two families. Both are read by the router:

**Astra AI Gateway connections** (a separate pooled connection layer):

`GW_GEMINI_API_KEYS`, `GW_GROQ_API_KEYS`, `GW_CLOUDFLARE_API_KEYS`, `GW_CLOUDFLARE_ACCOUNT_IDS`, `GW_BEDROCK_API_KEYS`, `GW_OPENROUTER_API_KEYS`, `GW_MISTRAL_API_KEYS`, `GW_CEREBRAS_API_KEYS`, `GW_SAMBANOVA_API_KEYS`, `GW_COHERE_API_KEYS`, `GW_ZAI_API_KEYS`

**Routed AI providers** (used directly by `AstraRouter`):

`GEMINI_API_KEYS`, `GROQ_API_KEYS`, `CLOUDFLARE_API_KEYS`, `CLOUDFLARE_ACCOUNT_IDS`, `BEDROCK_API_KEYS`, `OPENROUTER_API_KEYS`, `MISTRAL_API_KEYS`, `CEREBRAS_API_KEYS`, `SAMBA_API_KEYS`, `COHERE_API_KEYS`, `ZAI_API_KEYS`

Each accepts a comma-separated list, so you can add a second key to the same line:

```dotenv
GROQ_API_KEYS=your-first-key,your-second-key
```

Some providers need more than a key:

- **Cloudflare** — `CLOUDFLARE_API_KEYS` plus `CLOUDFLARE_ACCOUNT_IDS` (the same for the `GW_` pair).
- **Amazon Bedrock** — `BEDROCK_API_KEYS` plus a region (`AWS_REGION`, already set to `us-east-1` in the template). `BEDROCK_CREDENTIALS` exists for the `access_key:secret_key` form and is commented out.

**Image generation** is configured separately and is optional: `IMAGE_CLOUDFLARE_API_KEY` + `IMAGE_CLOUDFLARE_ACCOUNT_ID`, `IMAGE_OPENROUTER_API_KEY`, or `IMAGE_GEMINI_API_KEY`.

> Never paste a real key into a ticket, a screenshot, or a commit. Replace the placeholder text on the right-hand side of the `=` with the key your provider issued you.

### 4.3 Models and endpoints

`.env.example` also lists each provider's models and base URL, for example:

```dotenv
GEMINI_MODELS=gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
```

These defaults are already filled in and are fine for a first run. Change them only if your provider account does not offer a listed model, or if you want a different priority order. Note that the naming is not uniform across providers — SambaNova uses `SAMBA_MODELS` / `SAMBA_BASE_URL` / `SAMBA_API_KEYS`, while its Gateway counterparts use the longer `GW_SAMBANOVA_*` and `GW_SAMBA_*` spellings. Copy the exact names from the template.

### 4.4 Server settings in `.env`

The template's section 3 holds optional server settings. The two you are most likely to touch:

```dotenv
PORT=8787
BIND=127.0.0.1
```

Configuration precedence in `astra/core/config.py` is: **environment variable → `config.json` → `.env` → built-in default**. So a `$env:PORT=...` set in your shell wins over `.env` for that session.

### 4.5 Required vs optional

| Variable | Required? | Notes |
| --- | --- | --- |
| A provider key (`GEMINI_API_KEYS`, `GROQ_API_KEYS`, …) | Yes, one is needed for chat | Unset providers report "not configured" rather than "unhealthy" |
| `ASTRA_TOKEN` | No locally, **yes if you expose the server beyond your own machine** | Requires a token on every `/api/*` request; the bundled web UI does not send one |
| `PORT`, `BIND`, `NO_BROWSER` | No | Defaults `8787`, `127.0.0.1`, browser opens |
| `ASTRA_SCHEDULER` | No | Set to `1` to run the scheduler daemon |
| `RUNTIME_*` | No | Runtime backend tuning; see Section 7 |

### 4.6 Protecting your secrets

- **`.env` is already git-ignored.** The repository's `.gitignore` lists `.env`, `config.json`, `credentials/`, `keys/`, and `secrets/` — so a normal `git add` will not pick them up.
- Keep it that way: never run `git add -f .env`, and never paste the contents of `.env` into an issue or a PR description.
- If you use `ASTRA_TOKEN`, generate a long random value rather than a memorable one.
- Astra reads secrets from environment variables, `.env`, or `config.json`, and its responses are redacted before they leave the process — but the file on disk is only as protected as your account and disk.

### 4.7 Verify the configuration

After editing `.env`, start Astra and read the startup banner. It prints whether AI is configured and which providers are loaded:

```text
🧠 Chat pipeline ON (Gateway ... ) | Tools: ... | AI: configured — N set: gemini, groq
```

If it says `AI: not configured`, the key was not found — check that the variable name matches `.env.example` exactly and that the file is in the repository root.

You can also check programmatically once the server is running:

```powershell
Invoke-RestMethod http://localhost:8787/api/health | ConvertTo-Json -Depth 4
```

---

## 5. Start Astra

### 5.1 Recommended: PowerShell

From the repository root:

```powershell
.\start.ps1
```

The script verifies Python is on `PATH`, then runs `python run.py`. Keep this window open — it is the server's console.

### 5.2 Alternative: Command Prompt

```cmd
start.bat
```

### 5.3 Manual startup

Both scripts are thin wrappers. You can run the launcher directly:

```powershell
python run.py
```

### 5.4 Per-session options

Set the variable in the same PowerShell window before starting. These work because `Config` reads environment variables first:

```powershell
$env:PORT="9000"
$env:NO_BROWSER="1"
$env:BIND="0.0.0.0"
$env:ASTRA_SCHEDULER="1"
.\start.ps1
```

`start.ps1`'s own header documents `$env:PORT="9000"` and `$env:ASTRA_SCHEDULER="1"` as examples. `NO_BROWSER=1` suppresses the automatic browser launch (`run.py` opens it after ~0.8 s unless that is set).

### 5.5 What happens at startup

`run.py` prints a banner, then serves on the configured host and port:

```text
  🚀 Astra AI Agent running (FastAPI/ASGI)
  👉 Open:  http://localhost:8787/
  🔒 Bind: 127.0.0.1 | API auth: OPEN (set ASTRA_TOKEN)
  🧠 Chat pipeline ON (...) | Tools: ... | AI: configured — ...
  (Ctrl+C to stop)
```

- **Default port:** `8787`
- **Default bind:** `127.0.0.1` — loopback only, so nothing outside your machine can reach it
- **Browser:** opens automatically unless `NO_BROWSER=1`

### 5.6 Open the web interface

With no token configured, open:

```text
http://localhost:8787/
```

If you set `ASTRA_TOKEN`, every `/api/*` route requires it, supplied as an `Authorization: Bearer <token>` header, an `X-Astra-Token` header, or a `?token=` query parameter. Note that the bundled web UI does not send a token — it calls the API without one — so with `ASTRA_TOKEN` set the browser UI's API calls are rejected and you will need to supply the token yourself in a client that can send headers. `ASTRA_TOKEN` is best treated as a protection for direct/API access on a trusted network, not as something that makes the bundled UI usable behind it.

### 5.7 LAN access — read this before changing BIND

The default `127.0.0.1` is deliberate and safe. Setting `BIND=0.0.0.0` makes Astra listen on every network interface, which exposes the whole API — chat, tools, file operations, Web3 endpoints — to anyone who can route to your machine.

If you need LAN access:

```powershell
$env:BIND="0.0.0.0"
$env:ASTRA_TOKEN="<a long random secret>"
.\start.ps1
```

Always set `ASTRA_TOKEN` before changing the bind address, and keep the server on a trusted network — never port-forward it to the public internet.

Be aware of the trade-off this creates in this repository: the bundled web UI does not attach a token to its requests, so setting `ASTRA_TOKEN` protects the API but the in-browser interface's calls will be rejected until a token is supplied.

### 5.8 Stopping Astra safely

Press **Ctrl+C** in the console window running the server. `run.py` catches `KeyboardInterrupt`, prints a farewell, stops the scheduler, and closes the store. Closing the window works too, but Ctrl+C is the clean shutdown.

---

## 6. First-Time Usage

### 6.1 Open the Assistant

Go to `http://localhost:8787/`. The main page has tabs for the Assistant, the Astra Agent Terminal, Providers, Router, Workflow, System Health/Logs, Web3, and Backup — the Agent Runtime is what backs the Terminal tab.

### 6.2 Send your first message

In the Assistant, type a simple message and press Enter:

```text
Hello! What can you help me with?
```

If a provider key is configured, you get a reply. If not, the reply explains that AI is not configured — go back to Section 4.

### 6.3 Verify provider health

Check the **Providers** tab, which shows provider health, latency, and cost, and can run a test against providers and models. To check from the shell instead:

```powershell
Invoke-RestMethod http://localhost:8787/api/health | ConvertTo-Json -Depth 4
Invoke-RestMethod http://localhost:8787/api/providers | ConvertTo-Json -Depth 4
```

`/api/health` reports the database, provider, and scheduler checks. A provider with no API key is reported as *not configured*, which does not make the overall health check fail; a provider that is configured but failing does.

### 6.4 Try a tool

Ask the Assistant to do something that uses a tool, for example:

```text
What files are in the current directory?
```

Tool use requires the Agent Runtime. Without WSL2 installed, the runtime reports itself unavailable and the operation fails closed rather than running on your Windows machine — that is expected behaviour, and Section 7 explains how to enable it.

---

## 7. Windows Agent Runtime Setup

### 7.1 Web application vs Agent Runtime

These are two separate things, and it is worth being precise about the difference:

| | Astra web application | Astra Agent Runtime |
| --- | --- | --- |
| What it is | FastAPI/uvicorn server + browser UI | An isolated Linux environment |
| Where it runs | Native Windows process | Inside WSL2 Ubuntu |
| What it does | Serves the UI, chat, tools, Web3 | Executes every agent-issued shell command, file operation, and package install |
| Requires WSL2 | No | **Yes** |

The runtime exists because an agent running `npm install` or a project's test suite should not do it on your Windows host. Instead, agent operations execute inside WSL2 Ubuntu in a structured isolation model:
- **Per-runtime isolated directories:** The `/workspace` (project root), `/root` (user home directory), and `/tmp` (scratch space) directories are separate for each runtime, so different runtimes do not share project state or temporary files.
- **User-scope package privacy:** Package managers are pointed at the runtime's private home `/root` (`PYTHONUSERBASE=/root/.local`, `NPM_CONFIG_PREFIX=/root/.npm-global`, `CARGO_HOME`, `GOPATH`, `GEM_HOME`, and the XDG directories), so a **user-scope** install lands in that runtime's own writable state and is invisible to other runtimes. Note that Astra does not force `--user` on every pip call (that would break `python3 -m venv`); Astra's own package installer passes `--user` explicitly, and `PYTHONUSERBASE` decides where it lands. A bare `pip install <pkg>` you type yourself is an ordinary system install and is **not** private.
- **Shared system rootfs:** System-level package installations (like `sudo apt install`) write to the underlying Ubuntu root filesystem, which is shared among all runtimes by default (`RUNTIME_ROOTFS_MODE=shared`). On Windows this is fixed: the WSL2 backend always reports `shared` and does not read `RUNTIME_ROOTFS_MODE`, so system packages installed by one runtime are visible to others using the same distribution. (The `copy` mode that clones a rootfs per runtime is a proot/Termux feature only.)
- **Per-session process state:** Terminal/PTY sessions, active processes, shell history, and current working directories are isolated and managed strictly per-session.
- **Host-filesystem isolation:** Outward access to Windows filesystems is blocked entirely. All host drives (like `C:\` at `/mnt/c`) and WSL system mounts are unmounted inside the session's private mount namespace.

### 7.2 How Astra reaches the runtime

```text
Astra Agent Terminal  ->  Astra Runtime  ->  wsl.exe  ->  WSL2  ->  Ubuntu  ->  bash
```

`wsl.exe` is **transport only** — it carries the command into Ubuntu. Every command the agent runs is executed by Ubuntu's own `bash`, inside the distribution, and the agent only ever sees a `/workspace` prompt.

**PowerShell and `cmd.exe` are not runtime backends.** Nothing in the agent execution path invokes them, and they are not on the guest `PATH`.

### 7.3 Isolation, verified rather than assumed

Each session runs in a **private mount namespace** (`unshare -m --propagation private`). Inside it:

- Every host-provided mount (`/mnt/c`, `/mnt/wsl`, `/mnt/wslg`, Windows drives — anything of type `9p`, `drvfs`, `virtiofs`, `v9fs`) is unmounted, so `C:\` and your Windows profile are unreachable.
- The runtime's own directories are bind-mounted onto `/workspace`, `/root`, and `/tmp`. That is what isolates your workspace files, scratch files, and user-scope package state per runtime — but it is not a private copy of Ubuntu: system-level packages installed with `apt` land in the distribution itself and are shared by every runtime using it.
- `PATH` is set to the Linux-only guest path; `powershell.exe` and `cmd.exe` are not on it.
- The environment is rebuilt from scratch with `env -i`, so no Windows environment variable (and no host secret) leaks in.

Because the namespace is private, **your own `wsl` shell keeps its mounts untouched**.

On startup the runtime runs a real probe that verifies isolation rather than assuming it. If any check fails, the runtime reports itself unavailable with the reason.

### 7.4 Installation and initialization — what actually happens

You do not run a command to create or start the runtime. Astra creates it **on demand**: the first time an agent operation needs it, the runtime manager materialises the layout and probes the backend (`astra/runtime/manager.py`). If WSL2 Ubuntu is present and healthy, the runtime comes up; if not, it reports unavailable.

There is also no shell command in this repository that starts or creates the runtime directly. The runtime lifecycle is driven from the Astra Agent Terminal's overflow menu (`⋮`), which exposes **Runtime status**, **Restart runtime**, **Stop runtime**, and **Kill this session**; and the same lifecycle is exposed over HTTP to the web UI:

| Action | Endpoint | Effect |
| --- | --- | --- |
| Status | `GET /api/runtime/status` | Availability, backend, distro, shell, live sessions |
| Lifecycle | `POST /api/runtime/lifecycle` | Body `{"action": "..."}` with `start`, `stop`, `restart`, `create`, `reset`, or `destroy` |

> **`reset` and `destroy` delete data.** `reset` wipes the workspace, uploads, and temp files; `destroy` deletes the entire runtime directory and everything in it. Neither runs on its own — they only happen when explicitly requested.

### 7.5 Verification

**Through the UI:** open the Astra Agent Terminal tab. The status line at the bottom shows the live connection state, and when the runtime is available the header names the backend — on Windows it reads something like `Ubuntu · WSL2`, with the working directory `/workspace`. The `⋮` menu's **Runtime status** item shows the full report, including the failure reason when the runtime is unavailable.

**From PowerShell:**

```powershell
Invoke-RestMethod http://localhost:8787/api/runtime/status | ConvertTo-Json -Depth 4
```

A healthy result has `"available": true`, `"backend": "wsl2"`, `"distro": "Ubuntu"`, and `"shell": "bash"`.

**End-to-end acceptance run** — the repository ships a real, unmocked acceptance script that drives the actual runtime and exits non-zero if any check fails. It is designed to be run manually when you want a full end-to-end verification:

```powershell
python scripts/runtime_acceptance.py
```

On Windows, this script automatically uses the WSL2 backend: `astra/runtime/backends/__init__.py` selects the WSL2 backend whenever the platform is detected as Windows and `RUNTIME_BACKEND` is either set to `auto` (the default) or left unset. Because the script uses its own configuration object (`_Cfg`) and does not read the `RUNTIME_BACKEND` environment variable, pinning the backend with `$env:RUNTIME_BACKEND="wsl2"` has no effect on this script.

It verifies the guest toolchain, a live PTY with resize, a verified package install, persistence across restart, per-runtime private state, host isolation (`/mnt/c` and `C:\` unreachable, `powershell.exe` and `cmd.exe` absent from the guest `PATH`), and Chat ↔ Terminal sharing one session.

> This script is not part of `pytest tests/` and needs a working runtime, so run it by hand when you want a real end-to-end check.

### 7.6 When the runtime is unavailable

If WSL2 or Ubuntu is missing, or the isolation probe fails, Astra prefixes its status with `Agent Runtime unavailable:` and states the specific reason, for example:

```text
Agent Runtime unavailable: WSL2 Ubuntu distribution is not installed (installed: Debian)
```

and **fails closed**. `runtime_start` and every execution tool raise rather than running anything on the host. A missing runtime never triggers a silent downgrade to Command Prompt or PowerShell.

### 7.7 Host terminal fallback and approval

There is one narrow path to host execution, and it is entirely separate from the runtime. The Agent's only host surface is the `host_terminal_request` tool, which **executes nothing**: it records a scoped request and returns `approval_required`. Execution happens only if you select **Allow** on the approval card in the Assistant chat.

The card shows the exact command, its working directory, the reason, and a host-execution warning, with **Deny** and **Allow** buttons. The rules:

- **Scoped, not a global switch.** An approval binds one conversation, one operation, the exact command, and its cwd. A different command cannot ride an existing approval.
- **Exactly once.** The approval is consumed on use, so a double click, a refresh, or a reconnect cannot run it twice.
- **Deny and expiry never run.** A denied or expired approval executes nothing, and the agent continues in the runtime where it can.
- **Approval-only UI.** The card appears in the Assistant chat only. The Astra Agent Terminal stays a pure terminal.
- **Short-lived.** A pending approval expires after `HOST_APPROVAL_TTL_S` (default 900 seconds / 15 minutes). A server restart drops still-pending cards — which is safe, because an approval that no longer exists can never execute.

```dotenv
# .env — optional, adjust the timeout in seconds
HOST_APPROVAL_TTL_S=900
```

**Related setting:** `GRANTED_PERMISSIONS` controls which risk classes tools may use. Astra grants `read`, `low_risk_write`, `browser_action`, and `system_action` by default, which is what lets the runtime lifecycle tools (`start`, `stop`, `restart`, `reset`, `destroy`) and execution tools run at all. Removing one makes every tool in that class fail closed with "denied by policy".

### 7.8 Runtime configuration

All of these are optional; the defaults are what most Windows users want. Names are verified against `.env.example` and the WSL backend:

```dotenv
RUNTIME_BACKEND=auto        # auto | wsl2 | proot  (auto = wsl2 on Windows)
RUNTIME_WSL_DISTRO=Ubuntu   # the distribution name as wsl.exe knows it
RUNTIME_WSL_ROOT=/var/lib/astra/runtime
RUNTIME_WSL_ISOLATE=1       # 1 = unmount every host mount (recommended)
RUNTIME_WSL_UMASK=000
RUNTIME_WSL_WORKSPACE=
RUNTIME_WSL_EXE=            # leave empty to auto-detect wsl.exe
```

> Do **not** set `RUNTIME_WSL_ISOLATE=0` to make a problem go away. It disables the host-filesystem unmounting — that is, the isolation itself. Fix the underlying cause instead (see 8.10).

---

## 8. Troubleshooting

### 8.1 `python` is not recognised

`setup.bat` and `start.bat` both stop with `python not found on PATH`.

1. Reinstall Python from <https://www.python.org/downloads/windows/> and tick **"Add python.exe to PATH"**.
2. Close and reopen the terminal — an already-open window keeps the old `PATH`.
3. Verify: `python --version`.

If several Pythons are installed, `py --version` and `py run.py` select a specific one.

### 8.2 Dependencies will not install

```powershell
python -m pip install --upgrade -r requirements.txt
```

`setup.bat` prints the same command when its `pip install` step fails. Only `fastapi` and `uvicorn` are required.

**If `python run.py` prints "FastAPI/uvicorn are required but not installed"**, the interpreter running the server is not the one you installed into — a common cause of a Microsoft Store Python shim shadowing your install. Check with `where python` and reinstall with the PATH box ticked.

### 8.3 Port already in use

Port `8787` is taken by another program. Either change the port:

```dotenv
# .env
PORT=8788
```

or for one session only:

```powershell
$env:PORT="8788"
.\start.ps1
```

If `uvicorn` reports an `[Errno 10048]` bind error, stop the other process — or find the current listener with:

```powershell
netstat -ano | Select-String ":8787"
```

### 8.4 Startup banner says `AI: not configured`

The provider key was not picked up. Check that:

- the variable name matches `.env.example` exactly (for example `SAMBA_API_KEYS`, not `SAMBANOVA_API_KEYS`)
- `.env` is in the repository root, next to `run.py`
- the line is not commented out with `#`
- the value has no surrounding quotes or stray whitespace

### 8.5 Invalid or rejected API key

A provider that authenticates but fails reports `healthy: false` on the **Providers** tab and in `/api/providers`. Verify the key with the provider directly — most offer a free "list models" or quota endpoint. Common causes: a revoked key, an account with no credit, or a key copied with surrounding whitespace.

### 8.6 Provider connection failures

- Confirm outbound internet access and any proxy or firewall in front of the provider's base URL.
- Confirm the model's region or account tier actually offers it — a model listed in `.env.example` that your account cannot call will fail at request time.
- Test a different provider to isolate whether the problem is the provider or Astra.

### 8.7 PowerShell blocks the scripts

If `.\setup.ps1` or `.\start.ps1` is refused:

```powershell
Get-ExecutionPolicy -List
```

Set `RemoteSigned` for your user account (this allows local scripts, and scripts downloaded from the internet still require a signature):

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Confirm with `Y`, then re-run the script. `Set-ExecutionPolicy -Scope CurrentUser` needs no administrator rights. If corporate policy forbids it, use the `.bat` scripts instead — they are unaffected.

### 8.8 WSL2 or Ubuntu missing

Reported as *"Agent Runtime unavailable: WSL2 Ubuntu is not installed"* (or, if Ubuntu is present under another name, *"WSL2 Ubuntu distribution is not installed (installed: …)"*).

```powershell
wsl --list --verbose
```

If nothing is listed, run `wsl --install -d Ubuntu` from an elevated PowerShell and complete Ubuntu's first-launch setup. If the distribution is listed with `VERSION 1`, convert it with `wsl --set-version Ubuntu 2`. If you already have a different distribution and want Astra to use it, set `RUNTIME_WSL_DISTRO` to that name. Restart Astra afterwards.

### 8.9 "distribution 'X' is WSL1"

The Astra runtime requires WSL2. Convert the distribution:

```powershell
wsl --set-version Ubuntu 2
```

### 8.10 "could not be isolated from the Windows filesystem"

Host mounts are still visible inside the session, so Astra refuses to run rather than accept a weaker boundary. This is almost always caused by `unshare` or `mount` being missing inside the distribution. Both are provided by the `util-linux` package on Ubuntu:

```powershell
wsl
```

then inside Ubuntu:

```bash
# Check which tools are actually missing first
command -v unshare mount bash python3

# Install util-linux (provides unshare and mount)
sudo apt update && sudo apt install -y util-linux
```

Restart Astra and check the runtime status again. Setting `RUNTIME_WSL_ISOLATE=0` would suppress the error by disabling the isolation itself — do not do that.

### 8.11 A required tool is missing inside the distribution

Reported as *"bash/unshare/mount/python3 is not installed inside Ubuntu (required for …)"*. The message names the specific tool that is missing. On Ubuntu:

| Missing tool | Provided by | Install with |
| --- | --- | --- |
| `unshare`, `mount` | `util-linux` | `sudo apt install -y util-linux` |
| `bash` | `bash` | `sudo apt install -y bash` |
| `python3` | `python3` | `sudo apt install -y python3` |

Install only the packages for tools that are actually missing, then restart Astra:

```bash
sudo apt update
sudo apt install -y <package-name>
```

A runtime's own package bootstrap handles a missing `pip`, but `bash`, `unshare`, `mount`, and `python3` come from the base distribution.

### 8.12 Runtime is unavailable for another reason

Open the Astra Agent Terminal tab and use **⋮ → Runtime status** — it reports the specific reason. `GET /api/runtime/status` returns the same data as JSON.

For an end-to-end check against the real runtime, run the acceptance script directly (it will use the WSL2 backend automatically on Windows):

```powershell
python scripts/runtime_acceptance.py
```

A runtime reported as available at process start but stopped afterwards is expected: the previous process's PTY sessions are gone, while its filesystem persists.

### 8.13 Runtime data directories

The runtime keeps its files under `RUNTIME_DIR` (default `~/.astra/runtime`, i.e. `C:\Users\<you>\.astra\runtime`) and inside Ubuntu under `RUNTIME_WSL_ROOT` (default `/var/lib/astra/runtime`). Neither is a port. To relocate the host-side directory, set `RUNTIME_DIR`; to relocate the guest tree, set `RUNTIME_WSL_ROOT`.

### 8.14 Browser does not open

Expected when `NO_BROWSER=1` is set. Open `http://localhost:8787/` manually.

---

## 9. Updating Astra

### 9.1 Before you update

`.env` and `config.json` are git-ignored, so `git pull` will not touch them — but back them up anyway, along with the `data/` directory, which holds the local database. Run these from the repository root:

```powershell
# Create a backup folder (timestamped so it never overwrites an older backup)
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$backupDir = "astra-backup-$stamp"
New-Item -ItemType Directory -Path $backupDir | Out-Null

# Back up .env if it exists
if (Test-Path .env) {
    Copy-Item .env $backupDir
    Write-Host "Backed up .env"
} else {
    Write-Host "No .env found - skipping"
}

# Back up config.json if it exists
if (Test-Path config.json) {
    Copy-Item config.json $backupDir
    Write-Host "Backed up config.json"
} else {
    Write-Host "No config.json found - skipping"
}

# Back up the data/ directory if it exists
if (Test-Path data) {
    Copy-Item data $backupDir -Recurse
    Write-Host "Backed up data\"
} else {
    Write-Host "No data\ directory found - skipping"
}

Write-Host "Backup saved to: $backupDir"
```

The backup folder is created **inside the repository root** as `astra-backup-<timestamp>/`, and it is a plain copy — the `.gitignore` rules that protect `.env` and `config.json` do not cover a folder like this, because the copies sit at new paths. So **do not commit it**. Move the folder somewhere outside the repository (or delete it once you have confirmed the update worked) before you run any `git add -A`.

If you use the Agent Runtime, its user data lives outside the repository (under `RUNTIME_DIR`, and inside Ubuntu under `RUNTIME_WSL_ROOT`) and is not touched by an update either.

### 9.2 Update

Stop Astra first, then from the repository root:

```powershell
git fetch origin
git checkout main
git pull origin main
```

If your local branch has diverged and you have no local commits worth keeping, `git reset --hard origin/main` re-syncs it — but that discards uncommitted changes, so commit or stash anything you care about first.

### 9.3 After updating

If `requirements.txt` changed, re-run the install:

```powershell
python -m pip install --upgrade -r requirements.txt
```

Then start Astra again and confirm the banner looks normal. Your `.env` and `config.json` should be exactly as you left them.

---

## 10. Useful Commands

### 10.1 Install

| Purpose | Command | Source |
| --- | --- | --- |
| Clone | `git clone https://github.com/mainnetwallet/Astra-AI-Agent.git` | Git |
| Set up (CMD) | `setup.bat` | repository |
| Set up (PowerShell) | `.\setup.ps1` | repository |
| Install dependencies | `python -m pip install --upgrade -r requirements.txt` | repository |
| Create `.env` | `Copy-Item .env.example .env` | PowerShell |

### 10.2 Start and stop

| Purpose | Command | Source |
| --- | --- | --- |
| Start (PowerShell) | `.\start.ps1` | repository |
| Start (CMD) | `start.bat` | repository |
| Start directly | `python run.py` | repository |
| Custom port | `$env:PORT="9000"; .\start.ps1` | `start.ps1` header |
| No browser | `$env:NO_BROWSER="1"; .\start.ps1` | `run.py` |
| Scheduler daemon | `$env:ASTRA_SCHEDULER="1"; .\start.ps1` | `start.ps1` header |
| Stop | `Ctrl+C` in the server window | `run.py` |

### 10.3 Health checks

| Purpose | Command |
| --- | --- |
| Overall health | `Invoke-RestMethod http://localhost:8787/api/health` |
| Provider health | `Invoke-RestMethod http://localhost:8787/api/providers` |
| Runtime status | `Invoke-RestMethod http://localhost:8787/api/runtime/status` |
| Tool registry | `Invoke-RestMethod http://localhost:8787/api/tools` |
| Runtime acceptance run | `python scripts/runtime_acceptance.py` |

> If you set `ASTRA_TOKEN`, add the header: `Invoke-RestMethod http://localhost:8787/api/health -Headers @{ "X-Astra-Token" = "<your token>" }`

### 10.4 Windows and WSL checks

| Purpose | Command |
| --- | --- |
| Python | `python --version` |
| Git | `git --version` |
| WSL status | `wsl --status` |
| Installed distributions | `wsl --list --verbose` |
| Port 8787 listener | `netstat -ano \| Select-String ":8787"` |
| PowerShell policy | `Get-ExecutionPolicy -List` |

### 10.5 Updating

```powershell
git fetch origin
git checkout main
git pull origin main
```

---

## 11. Additional Documentation

| Document | Contents |
| --- | --- |
| [README.md](README.md) | Project overview, features, configuration reference |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Internal design: router, gateway, tool registry, storage |
| [docs/AGENT_RUNTIME.md](docs/AGENT_RUNTIME.md) | **Agent Runtime in depth** — isolation model, lifecycle, files and archives, package managers, security guards, troubleshooting, acceptance run |
| [Dockerfile](Dockerfile) | Container image definition (Linux, not Windows) |

Docker is **not** a Windows path in this repository — the provided `Dockerfile` is a Linux image and the Windows-specific path is the native `setup.bat` / `setup.ps1` + WSL2 flow described above.

---

## Summary

1. Install Python 3.9+ with PATH enabled; install Git.
2. Clone the repository, then run `.\setup.ps1` (or `setup.bat`).
3. Copy `.env.example` to `.env` and add a key for at least one AI provider, editing the file in Notepad.
4. Start with `.\start.ps1` and open `http://localhost:8787/`.
5. Install WSL2 + Ubuntu yourself if you want agent commands to execute; Astra will not do it for you and will not fall back to the Windows host.