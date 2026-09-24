"""Astra Agent Runtime - the isolated environment Agent work executes in.

Import surface:

* `RuntimeEngine`        - picks the runtime backend and locates its guest
* `AstraRuntimeUnavailable` - raised instead of ever falling back to the host
* `AgentRuntime`         - one isolated environment (filesystem, sessions, packages)
* `RuntimeManager`       - process-wide registry of runtimes
* `register_runtime_tools` - puts the runtime on the shared ToolRegistry

The Agent Runtime is ONE implementation with two backends:

    RuntimeEngine
        |
        +-- ProotRuntimeBackend   Android/Termux  -> proot + proot-distro
        +-- WslRuntimeBackend     Windows         -> WSL2 + Ubuntu

Only discovery, process bootstrap and directory layout differ between them;
sessions, the workspace, the file tools, package management, the PTY, the
BlobStore, execution history, SSE and the reconnect path are shared. Windows
CMD/PowerShell are NOT runtime backends and are never used for Agent
execution - see `astra/runtime/backends/base.py` for the shared guest
contract, `astra/runtime/engine.py` for the facade and
`docs/AGENT_RUNTIME.md` for the full model.
"""
from astra.runtime.backends import (BACKEND_AUTO, BACKEND_PROOT, BACKEND_WSL2,
                                    ProotRuntimeBackend, RuntimeBackend,
                                    WslRuntimeBackend, detect_platform,
                                    select_backend)
from astra.runtime.backends.base import GUEST_PATH, GUEST_SHELL, guest_env
from astra.runtime.engine import (GUEST_HOME, GUEST_TMP, GUEST_WORKSPACE,
                                  AstraRuntimeUnavailable, RuntimeEngine)
from astra.runtime.manager import (DEFAULT_RUNTIME_ID, AgentRuntime,
                                   RuntimeManager, strip_ansi)
from astra.runtime.tools import register_runtime_tools

__all__ = [
    "RuntimeEngine", "AstraRuntimeUnavailable", "AgentRuntime",
    "RuntimeManager", "register_runtime_tools", "GUEST_WORKSPACE",
    "GUEST_HOME", "GUEST_TMP", "GUEST_SHELL", "GUEST_PATH", "guest_env",
    "DEFAULT_RUNTIME_ID", "strip_ansi", "RuntimeBackend",
    "ProotRuntimeBackend", "WslRuntimeBackend", "select_backend",
    "detect_platform", "BACKEND_AUTO", "BACKEND_PROOT", "BACKEND_WSL2",
]
