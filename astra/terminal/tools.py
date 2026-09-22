"""Shared terminal tools for the ToolRegistry.

`register_terminal_tools(registry, manager)` puts the terminal on the ONE
tool surface every part of Astra already uses: the AI Gateway, every
Provider/model, the workflow engine and the agent tool loop all reach it
through ``ToolRegistry.execute("terminal_exec", ...)``. There is no
provider-specific terminal, and no parallel tool framework — just these
`Tool` objects, exactly like the file/git/browser/web3 tools.

Every tool resolves its session from (in priority order):

1. an explicit ``session_id`` argument,
2. ``ctx.terminal_session_id`` (set by the agent tool loop for the
   conversation being served),
3. the manager's default session.

That is what keeps an unrelated conversation's cwd/history out of this one.
"""
from __future__ import annotations

from astra.core.exceptions import ValidationError
from astra.core.permissions import Level
from astra.terminal.session import DEFAULT_TIMEOUT
from astra.tools.schemas import Tool

# A terminal command is arbitrary code execution; it is real system action,
# but requiring a per-command confirmation prompt would make an autonomous
# dev loop impossible. The permission-level gate is the real control: the
# operator grants (or withholds) `system_action` in GRANTED_PERMISSIONS.
TERMINAL_RISK = Level.SYSTEM_ACTION


def _manager(ctx, manager=None):
    if ctx is not None:
        monkey = getattr(ctx, "terminal", None)
        if monkey is not None:
            return monkey
    if manager is not None:
        return manager
    raise ValidationError("terminal manager not available in this context")


def _session_id(args, ctx):
    sid = args.get("session_id")
    if sid:
        return str(sid)
    if ctx is not None and getattr(ctx, "terminal_session_id", None):
        return str(ctx.terminal_session_id)
    return None


def _session(args, ctx, manager):
    mgr = _manager(ctx, manager)
    return mgr.get(_session_id(args, ctx), create=True,
                   cwd=args.get("cwd"))


def terminal_exec(args: dict, ctx=None, manager=None) -> dict:
    """Run a command in the persistent terminal session and wait for it.

    The session's cwd, environment and history carry over between calls, so
    `cd repo` then `pytest` behave like one continuous shell. A non-zero
    exit is a normal, structured result (status="failed" + exit_code +
    stderr) — not an exception — so the AI can read the failure, fix it and
    try again."""
    session = _session(args, ctx, manager)
    command = args.get("command", "")
    if not str(command).strip():
        raise ValidationError("command required")
    raw_timeout = args.get("timeout")
    timeout = (float(raw_timeout) if raw_timeout not in (None, "")
               else DEFAULT_TIMEOUT)
    return session.exec(command, timeout=timeout, wait=True,
                        env=args.get("env") or None)


def terminal_start(args: dict, ctx=None, manager=None) -> dict:
    """Start a long-running command (a dev server, a watcher, a build) in
    the background and return immediately with its process_id. Its output is
    captured; poll it with `terminal_status` and stop it with
    `terminal_stop`."""
    session = _session(args, ctx, manager)
    command = args.get("command", "")
    if not str(command).strip():
        raise ValidationError("command required")
    return session.exec(command, wait=False, env=args.get("env") or None)


def terminal_status(args: dict, ctx=None, manager=None) -> dict:
    """Status (and captured stdout/stderr) of a background process started
    with `terminal_start`."""
    session = _session(args, ctx, manager)
    process_id = args.get("process_id", "")
    if not process_id:
        raise ValidationError("process_id required")
    return session.status(str(process_id))


def terminal_stop(args: dict, ctx=None, manager=None) -> dict:
    """Stop a background process gracefully (SIGTERM / terminate), killing
    its whole process tree if it does not exit."""
    session = _session(args, ctx, manager)
    process_id = args.get("process_id", "")
    if not process_id:
        raise ValidationError("process_id required")
    return session.stop(str(process_id))


def terminal_kill(args: dict, ctx=None, manager=None) -> dict:
    """Force-kill a background process (SIGKILL)."""
    session = _session(args, ctx, manager)
    process_id = args.get("process_id", "")
    if not process_id:
        raise ValidationError("process_id required")
    return session.kill(str(process_id))


def terminal_history(args: dict, ctx=None, manager=None) -> dict:
    """The session's recent commands and their outcomes."""
    session = _session(args, ctx, manager)
    limit = int(args.get("limit", 20) or 20)
    return {"session_id": session.session_id,
            "cwd": session.cwd,
            "shell": session.shell.get("name", ""),
            "commands": session.history(limit=limit)}


def terminal_sessions(args: dict, ctx=None, manager=None) -> dict:
    """List the live terminal sessions and their cwd/processes."""
    mgr = _manager(ctx, manager)
    return {"sessions": mgr.snapshots()}


def terminal_output_read(args: dict, ctx=None, manager=None) -> dict:
    """Retrieve more of a command's full stdout/stderr after `terminal_exec`
    / `terminal_status` reported `truncated: true`. Pass the
    `stdout_blob_id`/`stderr_blob_id` it returned; chunked via
    `offset`/`next_offset` until `done`."""
    mgr = _manager(ctx, manager)
    blob_id = args.get("blob_id", "")
    if not blob_id:
        raise ValidationError("blob_id required (stdout_blob_id or "
                              "stderr_blob_id from a prior terminal result)")
    return mgr.blobs.read(blob_id, offset=int(args.get("offset", 0) or 0),
                          length=int(args.get("length", 6000) or 6000))


def terminal_history_read(args: dict, ctx=None, manager=None) -> dict:
    """Page back through this session's FULL command history — every
    command ever run here, not just the recent ones `terminal_history`
    keeps hot. Pass `offset`/`next_offset` from the previous call to
    continue; each entry is one JSON object (command/status/exit_code/
    stdout+stderr preview/blob ids for their full output)."""
    session = _session(args, ctx, manager)
    chunk = session.history_log_chunk(
        offset=int(args.get("offset", 0) or 0),
        length=int(args.get("length", 6000) or 6000))
    if chunk.get("status") != "ok":
        return chunk
    import json
    text = chunk.get("text", "")
    offset = chunk["offset"]
    total = chunk["total_chars"]
    # A chunk boundary can land mid-line; only WHOLE lines are parsed, and
    # `next_offset` only advances past whatever was actually consumed — a
    # partial trailing line is left for the next call, never guessed at.
    nl = text.rfind("\n")
    if nl == -1:
        # No complete line fit in this chunk — do not advance; ask the
        # caller to retry with a larger `length` instead of losing data.
        return {"status": "ok", "session_id": session.session_id,
                "entries": [], "offset": offset, "next_offset": offset,
                "done": offset >= total, "total_chars": total,
                "note": ("no complete history line fit in this chunk; "
                        "retry with a larger length")}
    usable = text[:nl + 1]
    entries = []
    for line in usable.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    next_offset = offset + len(usable)
    done = next_offset >= total
    return {"status": "ok", "session_id": session.session_id,
            "entries": entries, "offset": offset,
            "next_offset": None if done else next_offset, "done": done,
            "total_chars": total}


def terminal_close(args: dict, ctx=None, manager=None) -> dict:
    """Close a terminal session and kill any processes it started. Defaults
    to the current session."""
    mgr = _manager(ctx, manager)
    sid = _session_id(args, ctx)
    from astra.terminal.manager import DEFAULT_SESSION_ID
    target = sid or DEFAULT_SESSION_ID
    return {"closed": mgr.close(target), "session_id": target}


_SESSION_ARG = {"session_id": {"type": "string",
                                "description": "Terminal session id "
                                               "(defaults to the current one)"}}

TERMINAL_TOOLS = [
    ("terminal_exec", terminal_exec,
     "Run a command in the persistent terminal session (cwd/env/history "
     "carry over). Returns structured status/exit_code/stdout/stderr.",
     {"command": {"type": "string", "required": True},
      "cwd": {"type": "string"}, "timeout": {"type": "number"},
      "env": {"type": "dict"}, **_SESSION_ARG}),
    ("terminal_start", terminal_start,
     "Start a long-running command in the background (dev server, watcher, "
     "build). Returns a process_id for terminal_status/terminal_stop.",
     {"command": {"type": "string", "required": True},
      "cwd": {"type": "string"}, "env": {"type": "dict"}, **_SESSION_ARG}),
    ("terminal_status", terminal_status,
     "Status and captured output of a background process.",
     {"process_id": {"type": "string", "required": True}, **_SESSION_ARG}),
    ("terminal_stop", terminal_stop,
     "Gracefully stop a background process and its process tree.",
     {"process_id": {"type": "string", "required": True}, **_SESSION_ARG}),
    ("terminal_kill", terminal_kill,
     "Force-kill a background process.",
     {"process_id": {"type": "string", "required": True}, **_SESSION_ARG}),
    ("terminal_history", terminal_history,
     "Recent commands run in the terminal session and their outcomes.",
     {"limit": {"type": "int"}, **_SESSION_ARG}),
    ("terminal_output_read", terminal_output_read,
     "Retrieve more of a command's full stdout/stderr after "
     "terminal_exec/terminal_status reported truncated=true. Pass the "
     "blob_id it returned; chunked via offset/next_offset until done.",
     {"blob_id": {"type": "string", "required": True},
      "offset": {"type": "int"}, "length": {"type": "int"}}),
    ("terminal_history_read", terminal_history_read,
     "Page back through this session's FULL command history (every "
     "command ever run here, beyond what terminal_history keeps hot). "
     "Chunked via offset/next_offset until done.",
     {"offset": {"type": "int"}, "length": {"type": "int"}, **_SESSION_ARG}),
    ("terminal_sessions", terminal_sessions,
     "List live terminal sessions with their cwd and processes.", {}),
    ("terminal_close", terminal_close,
     "Close a terminal session and kill its processes.", dict(_SESSION_ARG)),
]

# Tools that mutate session/process state — never blindly retried.
_IDEMPOTENT = {"terminal_status", "terminal_history", "terminal_sessions",
              "terminal_output_read", "terminal_history_read"}


def register_terminal_tools(reg, manager) -> int:
    """Register the terminal tools on `reg`. `manager` is a TerminalManager
    used whenever the ToolContext does not carry one."""
    for name, fn, description, schema in TERMINAL_TOOLS:
        reg.register(Tool(
            name=name,
            fn=_bind(fn, manager),
            description=description,
            category="terminal",
            input=schema,
            risk=TERMINAL_RISK,
            requires_confirmation=False,
            idempotent=name in _IDEMPOTENT,
            plugin="core"))
    return len(TERMINAL_TOOLS)


def _bind(fn, manager):
    """Bind the manager as a keyword so the tool fn keeps the canonical
    `(args, ctx)` signature the registry calls it with."""
    def bound(args, ctx=None):
        return fn(args, ctx, manager)
    bound.__name__ = fn.__name__
    bound.__doc__ = fn.__doc__
    return bound
