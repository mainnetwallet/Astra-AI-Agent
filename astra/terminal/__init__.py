"""Shared persistent Terminal capability for Astra.

One implementation, used by the AI Gateway and by every Provider/model
through `ToolRegistry` — see `astra.terminal.session.TerminalSession`,
`astra.terminal.manager.TerminalManager` and `astra.terminal.tools`.
"""
from astra.terminal.manager import (DEFAULT_SESSION_ID, TerminalManager,
                                    default_session_id_for)
from astra.terminal.session import (COMPLETED, FAILED, RUNNING, STOPPED,
                                    TIMEOUT, TerminalSession, detect_platform,
                                    detect_shell)
from astra.terminal.tools import register_terminal_tools

__all__ = [
    "TerminalManager", "TerminalSession", "register_terminal_tools",
    "detect_platform", "detect_shell", "default_session_id_for",
    "DEFAULT_SESSION_ID", "RUNNING", "COMPLETED", "FAILED", "STOPPED",
    "TIMEOUT",
]
