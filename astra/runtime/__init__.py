"""Astra Agent Runtime — the isolated environment Agent work executes in.

Import surface:

* `RuntimeEngine`        — locates proot + a distro rootfs, builds runtime argv
* `AstraRuntimeUnavailable` — raised instead of ever falling back to the host
* `AgentRuntime`         — one isolated environment (filesystem, sessions, packages)
* `RuntimeManager`       — process-wide registry of runtimes
* `register_runtime_tools` — puts the runtime on the shared ToolRegistry

See `astra/runtime/engine.py` for the isolation model and
`astra/runtime/manager.py` for lifecycle and Chat ↔ Terminal session sharing.
"""
from astra.runtime.engine import (GUEST_HOME, GUEST_TMP, GUEST_WORKSPACE,
                                  AstraRuntimeUnavailable, RuntimeEngine)
from astra.runtime.manager import (DEFAULT_RUNTIME_ID, AgentRuntime,
                                   RuntimeManager, strip_ansi)
from astra.runtime.tools import register_runtime_tools

__all__ = [
    "RuntimeEngine", "AstraRuntimeUnavailable", "AgentRuntime",
    "RuntimeManager", "register_runtime_tools", "GUEST_WORKSPACE",
    "GUEST_HOME", "GUEST_TMP", "DEFAULT_RUNTIME_ID", "strip_ansi",
]
