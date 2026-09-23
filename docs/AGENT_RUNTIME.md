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

1. **Agent work never runs on the host.** There is no host-shell fallback
   anywhere in `astra/runtime/`.
2. **If the runtime is unavailable, it fails closed.** `runtime_start` and
   every execution tool raise `AstraRuntimeUnavailable`; the UI shows
   "Agent Runtime unavailable"; nothing silently downgrades to the host.

---

## 2. Isolation model (what is actually used)

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

### Per-runtime private package state (default)

Private writable state is the **default**, not an option. Even with the
shared rootfs, `RuntimeEngine.build_argv` injects a user-scope environment
(`_user_scope_env`) that redirects every user-scope package manager into
the runtime's own `$HOME` — and `$HOME` (`/root`) is a per-runtime bind:

| Variable | Points at |
| --- | --- |
| `PIP_USER=1`, `PYTHONUSERBASE`, `PIP_CACHE_DIR` | `/root/.local`, `/root/.cache/pip` |
| `NPM_CONFIG_PREFIX`, `NPM_CONFIG_CACHE`, `NODE_PATH` | `/root/.npm-global`, `/root/.npm` |
| `CARGO_HOME`, `GOPATH`, `GEM_HOME` | `/root/.cargo`, `/root/go`, `/root/.gem` |
| `XDG_DATA_HOME` / `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` | `/root/.local/share` / `/root/.config` / `/root/.cache` |

`PATH` ends with `/root/.local/bin:/root/.npm-global/bin`, so a tool
installed by one runtime is both private to it *and* runnable inside it.
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
```

Large output is never put in an event; the Activity Log stays bounded while
the full stream lives in the BlobStore.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| "Agent Runtime unavailable" | `proot` missing, or no proot-distro container | install `proot-distro` and a container (`proot-distro install ubuntu`), or set `RUNTIME_CONTAINER` |
| Terminal opens then exits immediately | the rootfs has no `/bin/bash` | `RUNTIME_CONTAINER` points at a container without bash |
| Package install reports `verified: false` | the verifier failed (often no network inside the runtime) | check the returned `verification` block; the runtime is the only place network is used |
| Install says "not installed in the runtime" | the manager genuinely is absent and cannot be bootstrapped | install it in the runtime rootfs |
| Terminal shows a stale screen after the tab was hidden | a fit/resize happened while hidden | press *Reconnect* (resumes from the byte offset) |
| `runtime_reset` lost my project | `reset` is destructive by design | it does not run on chat completion; only explicit calls |

---

## 11. Verifying it yourself (real acceptance run)

`scripts/runtime_acceptance.py` is the operator-runnable end-to-end
acceptance test for everything this document claims. It drives the REAL
proot runtime (no mocks) and exits non-zero if any check fails:

```sh
python3 scripts/runtime_acceptance.py
RUNTIME_CONTAINER=alpine python3 scripts/runtime_acceptance.py
ASTRA_ACCEPTANCE_DIR=~/.astra/accept ASTRA_ACCEPTANCE_KEEP=1 \
    python3 scripts/runtime_acceptance.py
```

It checks, in order: the real toolchain inside the guest; a live PTY with
`echo` and a `TIOCSWINSZ` resize the guest observes via `tput`; package
detection, a **verified** install and an import check; persistence across a
restart; Runtime A/B private state in both directions; host isolation (host
file unreadable, Termux prefix invisible, host write refused, guest `PATH`
clean); Chat ↔ Terminal sharing ONE PTY; and that the legacy host
`terminal_exec` is refused for Agent execution while still working for
trusted internals.

The hermetic suite that runs on every `pytest tests/` is
`tests/test_runtime.py` + `tests/test_host_terminal_block.py`; the frontend
contract is `tests/js/terminal_assets.test.js` (`node --test tests/js/`).

## 12. Known limitations

* **One shared rootfs by default.** `RUNTIME_ROOTFS_MODE=shared` means two
  runtimes on the same container share globally installed packages;
  `/workspace`, `/root` and `/tmp` are still per-runtime. Use `copy` for
  full per-runtime package isolation (costs a rootfs clone).
* **proot, not a kernel namespace.** Android's kernel blocks
  `unshare`/user namespaces, so proot is the strongest available
  mechanism. It is real filesystem/process isolation, but it is not a
  hypervisor and not a hardened container runtime.
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
