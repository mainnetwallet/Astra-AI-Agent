"""Agent Runtime tools for the shared ToolRegistry.

These are the ONLY way an AI reaches the Agent Runtime, and they are
registered on the same `ToolRegistry` that already carries the builtin,
browser, terminal and web3 tools — there is no second registry and no
parallel execution path (spec §13).

Every tool resolves its runtime from the ToolContext (`ctx.runtime`) and
the conversation's session id (`ctx.runtime_session_id`), so the chat and
the Astra Agent Terminal drive the same runtime and, when the ids match,
the very same PTY session.

Failure contract: when the isolated runtime is unavailable these tools
raise `AstraRuntimeUnavailable` (which ToolRegistry surfaces as a failed
tool call). They never fall back to the host shell.
"""
from __future__ import annotations

import base64
import binascii
import os

from astra.core.exceptions import ValidationError
from astra.core.permissions import Level
from astra.runtime.engine import GUEST_WORKSPACE
from astra.tools.schemas import Tool

RUNTIME_RISK = Level.SYSTEM_ACTION
FILE_RISK = Level.LOW_RISK_WRITE
READ_RISK = Level.READ


def _manager(ctx, manager=None):
    if ctx is not None and getattr(ctx, "runtime", None) is not None:
        return ctx.runtime
    if manager is not None:
        return manager
    raise ValidationError("agent runtime not available in this context")


def _runtime(args, ctx, manager):
    mgr = _manager(ctx, manager)
    runtime_id = args.get("runtime_id")
    return mgr.get(runtime_id) if runtime_id else mgr.default()


def _session_id(args, ctx):
    sid = args.get("session_id")
    if sid:
        return str(sid)
    if ctx is not None and getattr(ctx, "runtime_session_id", None):
        return str(ctx.runtime_session_id)
    return ""


# -- lifecycle ---------------------------------------------------------------

def runtime_status(args, ctx=None, manager=None) -> dict:
    """Current state of the isolated Agent Runtime: availability, backend,
    container, rootfs, live PTY sessions and detected tool versions."""
    runtime = _runtime(args, ctx, manager)
    status = runtime.status()
    if bool(args.get("refresh_capabilities")):
        status["capabilities"] = runtime.capabilities(refresh=True)
    return status


def runtime_create(args, ctx=None, manager=None) -> dict:
    """Create the runtime's filesystem layout (workspace/home/tmp). Idempotent.
    Pass reset=true to wipe existing runtime user data first."""
    runtime = _runtime(args, ctx, manager)
    return runtime.create(reset=bool(args.get("reset")))


def runtime_start(args, ctx=None, manager=None) -> dict:
    """Start the runtime and probe its capabilities. Raises when the
    isolated runtime is unavailable — there is no host fallback."""
    return _runtime(args, ctx, manager).start()


def runtime_stop(args, ctx=None, manager=None) -> dict:
    """Stop the runtime: close every PTY session. Files and installed
    packages persist."""
    return _runtime(args, ctx, manager).stop()


def runtime_restart(args, ctx=None, manager=None) -> dict:
    """Stop then start the runtime."""
    return _runtime(args, ctx, manager).restart()


def runtime_reset(args, ctx=None, manager=None) -> dict:
    """DESTRUCTIVE: wipe the runtime's workspace, uploads and temp files.
    Installed packages in the rootfs are kept."""
    return _runtime(args, ctx, manager).reset()


def runtime_destroy(args, ctx=None, manager=None) -> dict:
    """DESTRUCTIVE: delete the whole runtime directory."""
    return _runtime(args, ctx, manager).destroy()


# -- execution ---------------------------------------------------------------

def runtime_command(args, ctx=None, manager=None) -> dict:
    """Run a shell command INSIDE the Agent Runtime and return its output.

    The command executes in the runtime's own filesystem as the session's
    live shell, so `cd`/`export` persist between calls and the Astra Agent
    Terminal sees the same state. Nothing runs on the host."""
    runtime = _runtime(args, ctx, manager)
    command = args.get("command", "")
    if not str(command).strip():
        raise ValidationError("command required")
    raw_timeout = args.get("timeout")
    timeout = float(raw_timeout) if raw_timeout not in (None, "") else None
    return runtime.exec_command(
        command, session_id=_session_id(args, ctx), timeout=timeout,
        rows=int(args.get("rows") or 24), cols=int(args.get("cols") or 80))


def runtime_package_manager_detect(args, ctx=None, manager=None) -> dict:
    """Detect which package managers exist INSIDE the runtime (apt, apk,
    pip, npm, yarn, pnpm, git) and report their real versions."""
    runtime = _runtime(args, ctx, manager)
    return runtime.capabilities(refresh=bool(args.get("refresh", True)))


def runtime_package_install(args, ctx=None, manager=None) -> dict:
    """Install packages inside the runtime, then VERIFY the install.

    `ecosystem` is one of npm | pip | apt | apk | git. The result carries
    `installed` and `verified` separately: a command that exits 0 but whose
    verifier fails is reported as NOT installed."""
    runtime = _runtime(args, ctx, manager)
    ecosystem = str(args.get("ecosystem") or "").strip()
    packages = args.get("packages") or []
    if isinstance(packages, str):
        packages = [p for p in packages.replace(",", " ").split() if p]
    if not ecosystem:
        raise ValidationError("ecosystem required (npm|pip|apt|apk|git)")
    if not packages:
        raise ValidationError("packages required")
    raw_timeout = args.get("timeout")
    timeout = float(raw_timeout) if raw_timeout not in (None, "") else None
    return runtime.package_install(
        ecosystem=ecosystem, packages=list(packages),
        cwd=args.get("cwd") or GUEST_WORKSPACE,
        global_scope=bool(args.get("global")),
        **({"timeout": timeout} if timeout else {}))


# -- files -------------------------------------------------------------------

def runtime_directory_list(args, ctx=None, manager=None) -> dict:
    """List a directory inside the runtime (default /workspace)."""
    runtime = _runtime(args, ctx, manager)
    return runtime.list_directory(args.get("path") or GUEST_WORKSPACE,
                                  limit=int(args.get("limit") or 500))


def runtime_file_info(args, ctx=None, manager=None) -> dict:
    """Metadata for one path inside the runtime."""
    runtime = _runtime(args, ctx, manager)
    path = args.get("path")
    if not path:
        raise ValidationError("path required")
    return runtime.file_info(path)


def runtime_file_read(args, ctx=None, manager=None) -> dict:
    """Read a text file inside the runtime (bounded)."""
    runtime = _runtime(args, ctx, manager)
    path = args.get("path")
    if not path:
        raise ValidationError("path required")
    return runtime.read_file(path, max_bytes=int(args.get("max_bytes") or 200000))


def runtime_file_write(args, ctx=None, manager=None) -> dict:
    """Create or overwrite a text file inside the runtime."""
    runtime = _runtime(args, ctx, manager)
    path = args.get("path")
    if not path:
        raise ValidationError("path required")
    if "content" not in args:
        raise ValidationError("content required")
    return runtime.write_file(path, args.get("content", ""),
                              overwrite=bool(args.get("overwrite", True)))


def runtime_file_upload(args, ctx=None, manager=None) -> dict:
    """Write an uploaded file into the runtime from base64 content.

    The bytes are written to the destination INSIDE the runtime; archives
    are extracted with traversal/symlink protection via
    runtime_archive_extract."""
    runtime = _runtime(args, ctx, manager)
    filename = str(args.get("filename") or "").strip()
    encoded = args.get("content_base64")
    if not filename or not encoded:
        raise ValidationError("filename and content_base64 required")
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise ValidationError("filename must be a plain file name")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError("content_base64 is not valid base64")
    staging = os.path.join(runtime.uploads_dir, filename)
    os.makedirs(runtime.uploads_dir, exist_ok=True)
    if len(data) > runtime.paths.max_file_bytes:
        raise ValidationError("upload exceeds the runtime file size limit")
    with open(staging, "wb") as fh:
        fh.write(data)
    destination = args.get("destination") or ""
    if bool(args.get("extract")) and filename.lower().endswith(
            (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
             ".tar.xz", ".txz")):
        return runtime.import_and_extract(staging, destination)
    return runtime.import_file(staging, destination or filename)


def runtime_file_import(args, ctx=None, manager=None) -> dict:
    """Import a file already staged in the runtime's upload area (the web
    upload endpoint puts it there) into the runtime workspace. Host paths
    outside that staging directory are refused."""
    runtime = _runtime(args, ctx, manager)
    name = str(args.get("upload_id") or args.get("source_name") or "").strip()
    if not name:
        raise ValidationError("upload_id or source_name required")
    if "/" in name or "\\" in name:
        raise ValidationError("upload_id must be a plain file name")
    staging = os.path.join(runtime.uploads_dir, name)
    if not os.path.isfile(staging):
        raise ValidationError(f"no staged upload named '{name}'")
    destination = args.get("destination") or ""
    if bool(args.get("extract")):
        return runtime.import_and_extract(staging, destination,
                                          strip_components=int(
                                              args.get("strip_components") or 0))
    return runtime.import_file(staging, destination or name)


def runtime_file_copy(args, ctx=None, manager=None) -> dict:
    """Copy a file or directory inside the runtime."""
    runtime = _runtime(args, ctx, manager)
    source, destination = args.get("source"), args.get("destination")
    if not source or not destination:
        raise ValidationError("source and destination required")
    return runtime.copy(source, destination)


def runtime_file_move(args, ctx=None, manager=None) -> dict:
    """Move/rename a file or directory inside the runtime."""
    runtime = _runtime(args, ctx, manager)
    source, destination = args.get("source"), args.get("destination")
    if not source or not destination:
        raise ValidationError("source and destination required")
    return runtime.move(source, destination)


def runtime_file_remove(args, ctx=None, manager=None) -> dict:
    """Remove a file (or directory with recursive=true) inside the runtime."""
    runtime = _runtime(args, ctx, manager)
    path = args.get("path")
    if not path:
        raise ValidationError("path required")
    return runtime.remove(path, recursive=bool(args.get("recursive")))


def runtime_archive_extract(args, ctx=None, manager=None) -> dict:
    """Safely extract a zip/tar/tar.gz/tgz archive inside the runtime.

    Rejects absolute paths, `..` traversal and links escaping the
    destination, and enforces member-count/size limits."""
    runtime = _runtime(args, ctx, manager)
    archive = args.get("archive")
    if not archive:
        raise ValidationError("archive required")
    return runtime.extract_archive(
        archive, args.get("destination") or "",
        strip_components=int(args.get("strip_components") or 0))


_SESSION_ARG = {"session_id": {"type": "string",
                               "description": "Runtime terminal session id "
                                              "(defaults to this conversation's)"}}
_RUNTIME_ARG = {"runtime_id": {"type": "string",
                               "description": "Runtime instance (default: the "
                                              "default runtime)"}}

RUNTIME_TOOLS = [
    ("runtime_status", runtime_status,
     "Agent Runtime status: availability, backend, container, rootfs, live "
     "PTY sessions, detected tools.",
     {"refresh_capabilities": {"type": "bool"}, **_RUNTIME_ARG}, READ_RISK),
    ("runtime_create", runtime_create,
     "Create the Agent Runtime filesystem (workspace/home/tmp). Idempotent.",
     {"reset": {"type": "bool"}, **_RUNTIME_ARG}, FILE_RISK),
    ("runtime_start", runtime_start,
     "Start the Agent Runtime and probe capabilities. Fails (never falls "
     "back to the host) when isolation is unavailable.",
     dict(_RUNTIME_ARG), RUNTIME_RISK),
    ("runtime_stop", runtime_stop,
     "Stop the Agent Runtime and close its PTY sessions (data persists).",
     dict(_RUNTIME_ARG), RUNTIME_RISK),
    ("runtime_restart", runtime_restart,
     "Restart the Agent Runtime.", dict(_RUNTIME_ARG), RUNTIME_RISK),
    ("runtime_reset", runtime_reset,
     "DESTRUCTIVE: wipe the runtime workspace/uploads. Packages persist.",
     dict(_RUNTIME_ARG), RUNTIME_RISK),
    ("runtime_destroy", runtime_destroy,
     "DESTRUCTIVE: delete the entire Agent Runtime.", dict(_RUNTIME_ARG),
     RUNTIME_RISK),
    ("runtime_command", runtime_command,
     "Run a shell command INSIDE the isolated Agent Runtime (real PTY shell, "
     "cwd/env persist, shared with the Astra Agent Terminal). Returns "
     "status, exit_code and stdout.",
     {"command": {"type": "string", "required": True},
      "timeout": {"type": "number"}, "rows": {"type": "int"},
      "cols": {"type": "int"}, **_SESSION_ARG, **_RUNTIME_ARG}, RUNTIME_RISK),
    ("runtime_package_manager_detect", runtime_package_manager_detect,
     "Detect package managers available INSIDE the runtime with versions.",
     {"refresh": {"type": "bool"}, **_RUNTIME_ARG}, READ_RISK),
    ("runtime_package_install", runtime_package_install,
     "Install packages inside the runtime and verify the installation "
     "(npm|pip|apt|apk|git).",
     {"ecosystem": {"type": "string", "required": True},
      "packages": {"type": "list", "required": True},
      "global": {"type": "bool"}, "cwd": {"type": "string"},
      "timeout": {"type": "number"}, **_RUNTIME_ARG}, RUNTIME_RISK),
    ("runtime_directory_list", runtime_directory_list,
     "List a directory inside the runtime workspace.",
     {"path": {"type": "string"}, "limit": {"type": "int"}, **_RUNTIME_ARG},
     READ_RISK),
    ("runtime_file_info", runtime_file_info,
     "Metadata for a path inside the runtime.",
     {"path": {"type": "string", "required": True}, **_RUNTIME_ARG}, READ_RISK),
    ("runtime_file_read", runtime_file_read,
     "Read a text file inside the runtime.",
     {"path": {"type": "string", "required": True},
      "max_bytes": {"type": "int"}, **_RUNTIME_ARG}, READ_RISK),
    ("runtime_file_write", runtime_file_write,
     "Create or overwrite a text file inside the runtime.",
     {"path": {"type": "string", "required": True},
      "content": {"type": "string", "required": True},
      "overwrite": {"type": "bool"}, **_RUNTIME_ARG}, FILE_RISK),
    ("runtime_file_upload", runtime_file_upload,
     "Write a base64-encoded file into the runtime (optionally extracting "
     "it when it is an archive).",
     {"filename": {"type": "string", "required": True},
      "content_base64": {"type": "string", "required": True},
      "destination": {"type": "string"}, "extract": {"type": "bool"},
      **_RUNTIME_ARG}, FILE_RISK),
    ("runtime_file_import", runtime_file_import,
     "Import a file staged in the runtime upload area into the workspace.",
     {"upload_id": {"type": "string"}, "source_name": {"type": "string"},
      "destination": {"type": "string"}, "extract": {"type": "bool"},
      "strip_components": {"type": "int"}, **_RUNTIME_ARG}, FILE_RISK),
    ("runtime_file_copy", runtime_file_copy,
     "Copy a file/directory inside the runtime.",
     {"source": {"type": "string", "required": True},
      "destination": {"type": "string", "required": True}, **_RUNTIME_ARG},
     FILE_RISK),
    ("runtime_file_move", runtime_file_move,
     "Move/rename a file/directory inside the runtime.",
     {"source": {"type": "string", "required": True},
      "destination": {"type": "string", "required": True}, **_RUNTIME_ARG},
     FILE_RISK),
    ("runtime_file_remove", runtime_file_remove,
     "Remove a file/directory inside the runtime.",
     {"path": {"type": "string", "required": True},
      "recursive": {"type": "bool"}, **_RUNTIME_ARG}, FILE_RISK),
    ("runtime_archive_extract", runtime_archive_extract,
     "Safely extract a zip/tar/tar.gz/tgz archive inside the runtime "
     "(traversal and symlink escapes rejected).",
     {"archive": {"type": "string", "required": True},
      "destination": {"type": "string"},
      "strip_components": {"type": "int"}, **_RUNTIME_ARG}, FILE_RISK),
]

_IDEMPOTENT = {"runtime_status", "runtime_package_manager_detect",
               "runtime_directory_list", "runtime_file_info",
               "runtime_file_read"}


def register_runtime_tools(reg, manager) -> int:
    """Register the Agent Runtime tools on `reg`."""
    for name, fn, description, schema, risk in RUNTIME_TOOLS:
        reg.register(Tool(
            name=name,
            fn=_bind(fn, manager),
            description=description,
            category="runtime",
            input=schema,
            risk=risk,
            requires_confirmation=False,
            idempotent=name in _IDEMPOTENT,
            plugin="core"))
    return len(RUNTIME_TOOLS)


def _bind(fn, manager):
    def bound(args, ctx=None):
        return fn(args, ctx, manager)
    bound.__name__ = fn.__name__
    bound.__doc__ = fn.__doc__
    return bound
