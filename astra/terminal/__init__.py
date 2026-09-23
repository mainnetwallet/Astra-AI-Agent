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
# The approval-gated HOST fallback: the ONE path an Agent may take to a host
# command, and only after the user allows it in the Assistant Chat. See
# astra/terminal/approval.py + astra/terminal/fallback.py. Kept out of this
# module's import graph on purpose — bootstrap imports them explicitly so
# `astra.terminal` stays cheap for the host terminal itself.

__all__ = [
    "TerminalManager", "TerminalSession", "register_terminal_tools",
    "detect_platform", "detect_shell", "default_session_id_for",
    "DEFAULT_SESSION_ID", "RUNNING", "COMPLETED", "FAILED", "STOPPED",
    "TIMEOUT",
]

