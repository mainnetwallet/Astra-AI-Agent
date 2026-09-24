# Astra Agent Runtime & Astra Agent Terminal

The **Agent Runtime** is the isolated Linux environment every Agent action
executes in. The **Agent Terminal** is the PC-style terminal frontend
attached to it. This document covers both: the isolation model, the
lifecycle, files/packages, the Chat ↔ Terminal sharing contract, security
guards and troubleshooting.

---

## 1. Why it exists

Astra's host is Windows or Termux/Android. Neither is a place to run an
agent's `npm install`, `apt install` or a cloned project's test suite. The
runtime gives the agent its own filesystem, shell, Python, Node, npm, Git,
package managers, processes and PTY sessions.

Two rules are absolute:

1. **Agent work never *silently* runs on the host.** The runtime is always
   the first and default execution environment. The only host-execution
   path is a scoped, user-approved fallback (§8.1) — never a silent
   downgrade, and never a fallback because the runtime failed.
2. **If the runtime is unavailable, it fails closed.** `runtime_start` and
   every execution tool raise `AstraRuntimeUnavailable`; the UI shows
   "Agent Runtime unavailable"; nothing downgrades to the host on its own.

---

## 2. Isolation model (what is actually used)

**Two backends, one runtime.** Which mechanism provides the isolated Linux
userland depends on the host. Everything above the backend is shared:

```
    Astra Terminal                                              (UI)
          |
          v
    Astra Runtime  --------------------+                     RuntimeEngine
          |                            |
          v                            +--> ProotRuntimeBackend   Android/Termux
    wsl.exe (transport, Windows only)  |        -> proot + proot-distro
                                       |
                                       +--> WslRuntimeBackend     Windows PC
                                                -> WSL2 + Ubuntu
```

`RUNTIME_BACKEND=auto` (the default) selects **WSL2 on Windows** and
**proot everywhere else**. The choice is made once, in
`astra/runtime/backends/select_backend()`, and nothing above it - sessions,
the workspace, file operations, package management, the PTY, the BlobStore,
execution history, SSE, reconnect, the Gateway and the Providers - needs to
know which one is in use. An unknown or incompatible `RUNTIME_BACKEND`
produces a backend that reports itself UNAVAILABLE with a clear reason; it
never picks something else and never runs on the host shell.

## 2.1 Android / Termux - proot


**Backend: proot** — a userspace `chroot` + `mount --bind` + process
isolation implementation that works without root on Android/Termux. The
kernel does not allow `unshare`/user namespaces here, so proot is the
strongest mechanism available, and it is a real one: a separate rootfs, a
separate process tree, and an explicit bind set.

```
host (Termux/Android)
  └── proot --rootfs=<distro rootfs> --change-id=0:0
        ├── /dev /proc /sys            (kernel pseudo-filesystems only)
        ├── <runtime>/workspace  ->  /workspace   (the project root)
        ├── <runtime>/root       ->  /root        (shell state)
        └── <runtime>/tmp        ->  /tmp         (scratch)
```

Nothing else is bound. The host home, the Termux prefix (`$PREFIX/bin`),
`/sdcard`, `/storage`, `/system` and `/data/app` are **not** visible inside
the guest. Verified by `tests/test_runtime.py::TestRuntimeExecution`
(host file unreadable and unmodified after a guest write attempt; no host
binary reachable by absolute path) and by the argv contract test
(`test_argv_binds_only_the_runtime_dirs`), which asserts the guest side of
every bind is one of the allowed targets.

The guest environment is built with `env -i` — the host environment is
never inherited, so host secrets cannot leak into the runtime through the
environment.

### Rootfs

The base rootfs is the proot-distro container installed in the environment
(`$PREFIX/var/lib/proot-distro/containers/<container>/rootfs`, default
container `ubuntu`). `RUNTIME_ROOTFS_MODE=shared` (default) uses it directly
and gives each runtime its own `/workspace`, `/root` and `/tmp`;
`copy` clones the rootfs per runtime when globally installed packages must
also be per-runtime.

## 2.2 Windows PC - WSL2 + Ubuntu

The Astra process runs on Windows. Agent work does **not**: every command
executes inside WSL2 Ubuntu, and the shell behind the Astra Agent Terminal
is Ubuntu's `bash`.

```
Windows host                 |  Ubuntu (WSL2)
                             |
python -m astra              |
  Astra Runtime -- wsl.exe --+--> unshare -m (private mount ns)
                              |      |
                              |      +-> session bootstrap (host isolation,
                              |      |   and this runtime's dirs -> /workspace,
                              |      |   /root, /tmp)
                              |      |
                              |      +-> bash  (interactive)  or  command
                              |
   file tools <-- \\wsl.localhost\<distro>\var\lib\astra\runtime\<id>\...
```

* **`wsl.exe` is transport only.** Every invocation uses `-e` so the
  distribution's own shell never parses (and never expands) the command
  line. `wsl.exe` is never exposed to the Agent as a capability, and
  **`cmd.exe` and PowerShell are NOT runtime backends** - nothing in the
  Agent execution path invokes them.
* **Host isolation.** Each session runs in a PRIVATE mount namespace
  (`unshare -m --propagation private`) in which every host-provided mount
  (`fstype` `9p`/`drvfs`/`virtiofs`/`v9fs`: `/mnt/c`, `C:\`, `/mnt/wsl`,
  `/mnt/wslg`, `/usr/lib/wsl/drivers`, ...) is unmounted. The Windows
  filesystem, the Windows user profile, the Windows PATH and any Windows
  credential are therefore unreachable. The namespace is private, so the
  user's own `wsl` shell keeps its mounts untouched.
* **Layout.** The runtime's own tree lives inside Ubuntu
  (`RUNTIME_WSL_ROOT`, default `/var/lib/astra/runtime/<runtime-id>`) and is
  bind-mounted onto `/workspace`, `/root` and `/tmp`. The Astra process
  reaches the same files through `\\wsl.localhost\<distro>\...` for the
  file tools. Directories are created `0777` and sessions run `umask 000`,
  so files the (root) guest creates stay writable through the share. A
  host-side runtime root that is not the default one gets a tagged
  `<RUNTIME_WSL_ROOT>/h-<hash>` directory, so a test run or a second Astra
  installation can never collide with (or destroy) the real one.
* **Same environment as Android.** The session environment is built by the
  shared `env -i` builder in `backends/base.py`, so PATH, `HOME`, the Astra
  prompt and the per-runtime user-scope package redirection are identical
  on both platforms and cannot drift.
* **A real PTY, inside Ubuntu.** `astra/runtime/wsl_bridge.py` runs in the
  guest, allocates the pseudo-terminal with `pty.fork()`, applies
  `TIOCSWINSZ` for resize and delivers signals with `killpg`. The host side
  (`astra/runtime/pty_wsl.py`) is only a framed transport over `wsl.exe`'s
  pipes. `Ctrl+C` (raw `0x03` into the line discipline), `Ctrl+D`, `Ctrl+Z`,
  job control, ANSI colour, full-screen programs, long-running processes and
  reconnect therefore behave exactly as they do on Android.
* **Isolation is verified, not assumed.** `probe()` starts a real session
  and checks `uid=0`, both binds, a Linux-only PATH, and the absence of
  `powershell.exe`, `cmd.exe` and `/mnt/c`; any failure makes the runtime
  report itself unavailable with the reason.
* **Prerequisite, documented not automated:** WSL2 plus an Ubuntu
  distribution (`wsl --install -d Ubuntu`). Astra never installs WSL, never
  auto-invokes host execution, and if Ubuntu is missing the runtime reports
  *"Agent Runtime unavailable: WSL2 Ubuntu is not installed"* together with
  that hint.

Configuration (all in `.env.example`): `RUNTIME_BACKEND=auto|proot|wsl2`,
`RUNTIME_WSL_DISTRO` (default `Ubuntu`), `RUNTIME_WSL_ROOT`
(default `/var/lib/astra/runtime`), `RUNTIME_WSL_WORKSPACE`,
`RUNTIME_WSL_ISOLATE` (default `1`), `RUNTIME_WSL_UMASK` (default `000`) and
`RUNTIME_WSL_EXE`.


### Per-runtime private package state (default)

Private writable state is the **default**, not an option. Even with the
shared rootfs, `RuntimeEngine.build_argv` injects a user-scope environment
(`_user_scope_env`) that redirects every user-scope package manager into
the runtime's own `$HOME` — and `$HOME` (`/root`) is a per-runtime bind:

| Variable | Points at |
| --- | --- |
| `PYTHONUSERBASE`, `PIP_CACHE_DIR` | `/root/.local`, `/root/.cache/pip` |
| `NPM_CONFIG_PREFIX`, `NPM_CONFIG_CACHE`, `NODE_PATH` | `/root/.npm-global`, `/root/.npm` |
| `CARGO_HOME`, `GOPATH`, `GEM_HOME` | `/root/.cargo`, `/root/go`, `/root/.gem` |
| `XDG_DATA_HOME` / `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` | `/root/.local/share` / `/root/.config` / `/root/.cache` |

`PATH` ends with `/root/.local/bin:/root/.npm-global/bin`, so a tool
installed by one runtime is both private to it *and* runnable inside it.
`PIP_USER` is deliberately NOT exported: it would force `--user` on
every pip call, and pip REFUSES that inside a virtualenv, which would
break `python3 -m venv` + `pip install`. The runtime's own installer
passes `--user` explicitly (`packages.py::plan_install`), and
`PYTHONUSERBASE` decides where that lands, so the privacy property is
unchanged while venvs keep working.

Consequence: `pip install <pkg>` in runtime A is importable from A and
importable in A after A is restarted, and is **not** importable in
runtime B. Verified by `tests/test_runtime.py::TestPerRuntimeIsolation`.

`apt`/`apk` (system-level installs) still write into the shared rootfs
under `RUNTIME_ROOTFS_MODE=shared`; use `copy` when even that must be
private.

### Deliberately not a login shell

The runtime's shells are **not** login shells. The container ships
`/etc/profile.d/termux-profile.sh`, which appends the Termux prefix to
`PATH`; a login shell would put a host path on the guest's `PATH`. Instead
`build_argv` sets `PATH` explicitly to the guest's own
`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`.

---

## 3. Lifecycle

| Action | Effect | Data |
| --- | --- | --- |
| `create` | materialise workspace/home/tmp + state file | keeps data unless `reset=true` |
| `start` | probe capabilities, mark running | keeps everything |
| `stop` | close every PTY session | files, packages, installs persist |
| `restart` | stop then start | persists |
| `reset` | wipe workspace/uploads/tmp | **destroys user files** (rootfs packages kept) |
| `destroy` | delete the whole runtime directory | **destroys everything** |

Normal chat completion calls **none** of these — the runtime and its files
outlive every conversation turn (spec §2/§17). The state file
`runtime.json` records identity and the last known state; a runtime found
"running" at process start is reported as `stopped`, because the previous
process's PTYs are gone while its filesystem is what persists.

Instances live under `RUNTIME_DIR` (default `~/.astra/runtime/<id>/`).

---

## 4. The terminal

### Real PTY

`astra/runtime/pty.py` uses `pty.fork()`: a genuine master/slave pair with
the slave as the child's controlling terminal. That is why the shell
prompt, line editing, job control, ANSI colour, full-screen TUI programs,
`Ctrl+C` (the line discipline turns the raw `0x03` byte into `SIGINT` for
the foreground process group — never simulated), and `TIOCSWINSZ` resize
all behave like a desktop terminal.

Output is fanned out three ways: a capped in-memory replay buffer (what a
reconnecting browser re-draws), the shared **BlobStore** (the complete
stream, so a 500 MB build log never lands in RAM or the browser), and the
SSE feed.

The PTY OBJECT is the same class on both platforms, so nothing above it
changes. On Windows the pseudo-terminal is allocated INSIDE Ubuntu by
`astra/runtime/wsl_bridge.py` (also `pty.fork()`, `TIOCSWINSZ`, `killpg`)
and `astra/runtime/pty_wsl.py` is only the byte transport over `wsl.exe`'s
pipes: a small frame header for input, resize and signals, the raw terminal
stream for output. `Ctrl+C`/`Ctrl+D` are real bytes into the guest's line
discipline, not simulated signals.

### Frontend

`static/js/terminal.js` drives the pane; the emulator is **xterm.js**
(vendored) plus the fit addon. The pane is not a log view: input is raw
keystrokes, output is the PTY byte stream including escape sequences.

**The terminal is the UI.** The chrome is one header line (brand, tabs, and
a `⋮` overflow menu) and one status line (`● Connected · Agent Runtime ·
bash · /workspace · session conv-7`). There is no sidebar, no runtime card
and no dashboard consuming the viewport: `.at-stage` flexes to fill
everything the two lines do not, exactly like a desktop terminal. On
phones `.at-keys` adds a compact, Termux-style extra-key row (`ESC TAB
CTRL ALT / - HOME END ↑ ↓ ← → PGUP PGDN`); `CTRL`/`ALT` are sticky
modifiers, and the app height follows `visualViewport` so the software
keyboard never covers the prompt.

The file/workspace drawer (`.at-side`) and the runtime lifecycle actions
(`reconnect`, `restart session`, `clear`, `stop`, `kill`, `runtime status`)
live behind the `⋮` menu, hidden until asked for — they do not compete with
the terminal.

| Browser action | Endpoint | Effect |
| --- | --- | --- |
| keystroke | `POST /api/runtime/terminal/input` | bytes → PTY stdin |
| live output | `GET /api/runtime/terminal/stream` (SSE) | PTY stdout → `xterm.write()` |
| resize | `POST /api/runtime/terminal/resize` | `TIOCSWINSZ` on the real PTY |
| new tab | `POST /api/runtime/terminal/open` | new PTY session |
| close tab | `POST /api/runtime/terminal/close` | ends that session only |

Tabs are real sessions: rename (double-click), switch, close, reconnect.
Closing a UI tab never destroys the runtime.

**Keyboard**: `Enter`, `Backspace`, `Delete`, arrows, `Home`/`End`,
`PageUp`/`PageDown`, `Tab`, `Ctrl+C/D/L/Z/A/E/W/R`, `Shift`+arrows
selection, copy/paste — all delivered to the active PTY. `Ctrl+Shift+C/V/A`
is left to the browser for copy/paste/select-all.

**Reconnect**: the SSE feed resumes from a byte offset, so a dropped
connection re-draws without duplicating or losing screen content.

### Header & status bar

Runtime state, backend/container, connection state, shell, working
directory, session id and process state all come from the runtime — there
are no client-side timers or invented statuses. `at-led-on/off` reflects
the real SSE connection and PTY status.

---

## 5. Chat ↔ Terminal shared session

The join key is the session id. The chat pipeline derives `conv-<id>` for a
conversation (the same scheme `astra.terminal.manager.default_session_id_for`
already used) and the terminal opens `conv-<id>`. Same id ⇒ same
`PtyProcess` ⇒ same shell, cwd and filesystem.

`runtime_command` does not spawn a throwaway shell: it types the command
into the session's live PTY and waits for a completion sentinel. Its output
is redirected to a file inside the runtime which the host reads back
through the `/tmp` bind, so the result is never scraped from the
interactive stream (which would mean untangling shell echo, prompts and
ANSI redraws). `cd` and `export` therefore persist across calls, exactly as
in an interactive terminal.

Verified by `tests/test_runtime.py::test_chat_and_terminal_share_one_session`.

---

## 6. Files, uploads and archives

All paths are *guest* paths (`/workspace/...`). `RuntimePaths` maps them
onto the runtime's directories and refuses anything that would leave them —
checked on the resolved real path, so a symlink planted inside the
workspace that points outward is caught too.

Supported: list, info, read, write, mkdir, copy, move, remove, upload
(base64 or multipart), import from the staging area, and archive extraction
for `.zip`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tbz2`, `.tar.xz`,
`.txz`.

Archive guards (spec §19): member names are validated **before** anything
is written — absolute names and `..` traversal are rejected on the raw name
(so component-stripping cannot launder a hostile path), symlinks/hardlinks
are kept only when their target stays inside the destination, and member
count / total expanded size are bounded. Extraction runs in-process with
`zipfile`/`tarfile`; the host's `tar` is never executed.

Uploads are bounded by `RUNTIME_MAX_FILE_MB`, staged in the runtime's
`uploads/` directory (never bound into the guest), and copied
one-directionally — nothing is executed on the host.

---

## 7. Package managers

Detection is a single probe inside the runtime reporting the *real*
version of python3, pip, node, npm, yarn, pnpm, git, apt-get, apk, dpkg,
gcc, make, curl, wget. Nothing is reported as available unless it exists.

`runtime_package_install` supports `npm`, `pip`, `apt`, `apk` and `git`.
When `pip` is missing on a Debian/Ubuntu rootfs it is bootstrapped from the
runtime's own `apt-get` — a runtime-internal install, never a host one.

**Every install is verified** and `installed` and `verified` are reported
separately:

| Ecosystem | Verifier |
| --- | --- |
| npm (local) | `[ -d node_modules/<pkg> ]` |
| npm (global) | `npm ls -g --depth=0 <pkg>` |
| pip | `python3 -c "import <module>"` |
| apt | `dpkg -s <pkg>` |
| apk | `apk info -e <pkg>` |
| git | `<dest>/.git` + `git rev-parse --is-inside-work-tree` |

A command that exits 0 but whose verifier fails is reported as **not
installed**.

---

## 8. Tools (on the ONE ToolRegistry)

`astra/runtime/tools.py` registers these on the existing registry — there
is no second ToolRegistry:

`runtime_status`, `runtime_create`, `runtime_start`, `runtime_stop`,
`runtime_restart`, `runtime_reset`, `runtime_destroy`, `runtime_command`,
`runtime_package_manager_detect`, `runtime_package_install`,
`runtime_directory_list`, `runtime_file_info`, `runtime_file_read`,
`runtime_file_write`, `runtime_file_upload`, `runtime_file_import`,
`runtime_file_copy`, `runtime_file_move`, `runtime_file_remove`,
`runtime_archive_extract`.

Lifecycle/execution/install tools are `system_action` risk and therefore
gated by `GRANTED_PERMISSIONS`; reads are `read`; file mutations are
`low_risk_write`.

### The legacy host terminal is structurally blocked for Agent work

`astra/terminal/` still exists for Astra's own trusted internals, but every
tool it registers is marked **`agent_forbidden`** on its `ToolSchema`. The
guard is enforced in `ToolRegistry.execute` — the single path every tool
call takes — *not* by a system prompt:

```python
if tool.agent_forbidden and self._is_agent_execution(ctx):
    self._emit_tool("tool.blocked", tool, reason="agent_forbidden")
    return {"ok": False, "decision": "blocked", ...}   # nothing was spawned
```

`_is_agent_execution(ctx)` is true only when `ToolContext.agent_execution`
is set, which only `AgentToolLoop` and workflow steps do. So an Agent,
Provider or workflow that names `terminal_exec` gets a structured refusal
the model can read (`Decision: blocked`), no host process is started, and
`build_tool_catalog` never advertises a host tool to a model at all.
Regression coverage: `tests/test_host_terminal_block.py`.

### Host fallback — approval-gated, inside the Assistant Chat

The runtime is the primary environment and needs no permission. A host
command is possible *only* as an explicit, user-approved fallback:

```
AgentToolLoop -> HostTerminalFallback -> ApprovalManager
              -> Assistant-Chat approval -> trusted terminal_exec
```

There is deliberately **no** `AgentToolLoop -> terminal_exec` edge. The raw
host tools stay `agent_forbidden`; the Agent's only host surface is the
`host_terminal_request` tool (`astra/terminal/fallback.py`), which executes
**nothing** — it records a scoped approval and returns `approval_required`.
`ApprovalManager` (`astra/terminal/approval.py`) is the ONE place that runs
a host command, and only after the user selects **Allow**.

* **Scoped, not a global switch.** An approval binds one conversation, one
  request/operation, the *exact* command and its cwd. `decide()` runs the
  command stored on the request — a different command or cwd can never ride
  an existing approval.
* **Exactly once.** The `pending → approved` transition is the execution
  claim, taken under a lock, so a double click, a page refresh, an SSE
  reconnect or a retried request resolves to one execution.
* **Deny (and expiry) never run.** A denied or expired approval executes
  nothing; the Agent is told, and continues in the runtime where it can.
* **The decision lives in the Assistant Chat only.** The card shows the
  exact command, cwd, reason and a HOST-execution warning with Deny/Allow.
  The Astra Agent Terminal never shows approval UI — it stays a pure
  terminal. The final decision is written back into the card so a reloaded
  chat shows the resolved state.
* **No blocking thread.** A pending approval pauses the *logical* operation
  and resumes it (preserving conversation history, runtime session,
  execution scope and request/trace/op ids) when the user decides.

API (existing auth/session/security): `POST /api/terminal/approval` creates
a scoped request; `GET /api/terminal/approval/<id>` reads it; `GET
/api/terminal/approvals` lists pending ones; `POST
/api/terminal/approval/<id>` with `{"decision":"allow"|"deny"}` resolves it.
The approval card expires after `HOST_APPROVAL_TTL_S` (default 900s).

Regression coverage: `tests/test_host_fallback_approval.py`.

---

## 9. Events

Emitted on the shared EventBus (the Activity Log), never rendered as rows
inside the terminal:

```
runtime.created  runtime.started  runtime.stopped  runtime.failed
runtime.reset    runtime.destroyed
runtime.package.detect.started    runtime.package.detect.completed
runtime.package.install.started   runtime.package.install.completed
runtime.package.install.failed
runtime.file.import.started   runtime.file.import.completed
runtime.file.import.failed
runtime.archive.extract.started  runtime.archive.extract.completed
runtime.archive.extract.failed
terminal.started  terminal.command.started  terminal.completed
terminal.failed   terminal.stopped
host_terminal.approval_requested  host_terminal.approval_allowed
host_terminal.approval_denied     host_terminal.approval_expired
host_terminal.started  host_terminal.completed  host_terminal.failed
```

Large output is never put in an event; the Activity Log stays bounded while
the full stream lives in the BlobStore. Runtime execution events carry
`environment="agent_runtime"`; approved host execution carries
`environment="host"` plus its `approval_id` — so the two can never be
confused, and an unapproved host command has no events at all.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| "Agent Runtime unavailable" (Android/Termux) | `proot` missing, or no proot-distro container | install `proot-distro` and a container (`proot-distro install ubuntu`), or set `RUNTIME_CONTAINER` |
| "Agent Runtime unavailable: WSL2 Ubuntu is not installed" (Windows) | WSL2 or the Ubuntu distribution is missing | run `wsl --install -d Ubuntu` once (elevated), then reopen Astra. Astra never installs WSL for you and never falls back to CMD/PowerShell |
| "distribution 'X' is WSL1; the Astra runtime requires WSL2" (Windows) | the distribution is a WSL1 distro | `wsl --set-version <distro> 2` |
| "RUNTIME_BACKEND=<x> is not a known runtime backend" | a typo in the backend selection | use `auto`, `proot` or `wsl2` (or unset it) |
| "could not be isolated from the Windows filesystem" (Windows) | `unshare -m`/`umount` are unavailable inside the distribution | install `util-linux` inside Ubuntu, or set `RUNTIME_WSL_ISOLATE=0` only if you accept a weaker boundary |
| "powershell.exe is reachable from the runtime" (Windows) | the guest PATH picked up Windows entries | do not pass a Windows PATH in; the session environment is built from scratch - report it, since this is a security failure |
| Terminal opens then exits immediately | the rootfs has no `/bin/bash` | `RUNTIME_CONTAINER` points at a container without bash |
| Package install reports `verified: false` | the verifier failed (often no network inside the runtime) | check the returned `verification` block; the runtime is the only place network is used |
| Install says "not installed in the runtime" | the manager genuinely is absent and cannot be bootstrapped | install it in the runtime rootfs |
| Terminal shows a stale screen after the tab was hidden | a fit/resize happened while hidden | press *Reconnect* (resumes from the byte offset) |
| `runtime_reset` lost my project | `reset` is destructive by design | it does not run on chat completion; only explicit calls |

---

## 11. Verifying it yourself (real acceptance run)

`scripts/runtime_acceptance.py` is the operator-runnable end-to-end
acceptance test for everything this document claims. It drives the REAL
runtime on whichever backend the host provides (proot on Android/Termux,
WSL2 Ubuntu on Windows) - no mocks - and exits non-zero if any check fails:

```sh
python3 scripts/runtime_acceptance.py
RUNTIME_CONTAINER=alpine python3 scripts/runtime_acceptance.py
ASTRA_ACCEPTANCE_DIR=~/.astra/accept ASTRA_ACCEPTANCE_KEEP=1 \
    python3 scripts/runtime_acceptance.py
# Windows: pin the backend explicitly while diagnosing
RUNTIME_BACKEND=wsl2 python3 scripts/runtime_acceptance.py
RUNTIME_WSL_DISTRO=Ubuntu python3 scripts/runtime_acceptance.py
```

On Windows the same script additionally reproduces the isolation checks
that matter there: `/mnt/c` and `C:\` unreachable, `powershell.exe` and
`cmd.exe` not on the guest PATH, `which python3|node|npm|git` resolving
inside Ubuntu, and `pwd` reporting `/workspace`.

It checks, in order: the real toolchain inside the guest; a live PTY with
`echo` and a `TIOCSWINSZ` resize the guest observes via `tput`; package
detection, a **verified** install and an import check; persistence across a
restart; Runtime A/B private state in both directions; host isolation (host
file unreadable, Termux prefix invisible, host write refused, guest `PATH`
clean); Chat ↔ Terminal sharing ONE PTY; and that the legacy host
`terminal_exec` is refused for Agent execution while still working for
trusted internals.

The hermetic suite that runs on every `pytest tests/` is
`tests/test_runtime.py` + `tests/test_runtime_backends.py` (backend
detection/layout/launch, driven through the `WslRunner` seam so no WSL is
needed) + `tests/test_host_terminal_block.py`; the frontend contract is
`tests/js/terminal_assets.test.js` (`node --test tests/js/`).

## 12. Known limitations

* **One shared rootfs by default.** `RUNTIME_ROOTFS_MODE=shared` means two
  runtimes on the same container share globally installed packages;
  `/workspace`, `/root` and `/tmp` are still per-runtime. Use `copy` for
  full per-runtime package isolation (costs a rootfs clone).
* **proot, not a kernel namespace.** Android's kernel blocks
  `unshare`/user namespaces, so proot is the strongest available
  mechanism. It is real filesystem/process isolation, but it is not a
  hypervisor and not a hardened container runtime.
* **WSL interop cannot be unregistered from inside the namespace
  (Windows).** WSL provisions a global `binfmt_misc` handler (`WSLInterop`)
  plus `/init`, and a private mount namespace cannot unregister it. The
  measured impact is nil - nothing on Windows is bind-mounted into the
  session (every `9p`/`drvfs` mount is unmounted), no Windows directory is
  reachable, the guest PATH is Linux-only, and a Windows PE copied into the
  distribution fails to execute inside the session - but it is a shared
  kernel surface, not a hypervisor boundary. Treat the Windows runtime as
  strong process/filesystem isolation, not as a VM.
* **WSL2 + Ubuntu is a prerequisite on Windows.** Astra does not install
  it (`wsl --install -d Ubuntu` is a one-time, user-run step), and it does
  not silently substitute a host shell when it is missing.
* **No CPU/memory cgroup limits.** The kernel does not expose cgroups to
  apps here; the runtime enforces timeouts, output caps and
  archive/file/member limits instead (and the BlobStore keeps large output
  out of RAM).
* **Interactive-only programs through the terminal.** `runtime_command`
  types into the live shell and waits for a sentinel, which is right for
  commands; a program that expects its own interactive stdin (e.g. bare
  `python` or `vim`) should be driven from the Astra Agent Terminal pane.
* **Legacy host terminal still exists.** `astra/terminal/` (the
  `terminal_exec` family) remains for Astra's own diagnostics and is still
  registered as a tool — but it is `agent_forbidden`, never advertised to a
  model, and hard-blocked in `ToolRegistry.execute` for any Agent,
  Provider or workflow call (see §8). Agent *work* uses the runtime tools
  (`runtime_command`, `runtime_package_install`, …).
* **Host-fallback approvals are in-process.** Pending approvals live in
  memory (bounded, TTL-limited). A server restart drops a still-pending
  card — which is safe: an approval that is gone can never execute, and the
  user simply asks again. The *final* decision is persisted into the chat
  transcript, so a resolved card survives a reload.
