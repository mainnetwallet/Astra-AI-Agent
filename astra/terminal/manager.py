"""TerminalManager — the process-wide registry of persistent terminal
sessions.

One manager instance is created by ``astra.bootstrap.build`` and handed to
the shared ``ToolRegistry`` (via ``ToolContext``) and to the agent tool
loop. Because the Gateway and every Provider reach the terminal through
that same registry, they all see the same sessions: a ``cd`` performed by
one AI call is still in effect for the next call, whichever model made it.

Sessions are keyspaced by ``session_id``. The chat pipeline derives the id
from the conversation it is serving (``conv-<conversation_id>``), so two
unrelated conversations can never share terminal state, cwd or history.

Lifecycle: ``close_all`` is called on server shutdown; ``close_idle``
reaps sessions nothing has used for a while; ``close`` ends one session
and kills its background processes.
"""
from __future__ import annotations

import threading

from astra.terminal.session import TerminalSession

DEFAULT_SESSION_ID = "default"
DEFAULT_IDLE_SECONDS = 30 * 60


class TerminalManager:
    def __init__(self, *, events=None, config=None, default_cwd=None,
                 max_output_chars=None, history_limit=None,
                 idle_seconds: float = DEFAULT_IDLE_SECONDS, on_output=None):
        self.events = events
        self.config = config
        self.default_cwd = default_cwd
        self.idle_seconds = float(idle_seconds or 0)
        self.on_output = on_output
        self._max_output_chars = max_output_chars
        self._history_limit = history_limit
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = threading.RLock()

    # -- lookup / creation ---------------------------------------------------
    def get(self, session_id: str | None = None, *, create: bool = True,
            cwd: str | None = None, shell: dict | None = None,
            env: dict | None = None) -> TerminalSession | None:
        key = str(session_id or DEFAULT_SESSION_ID)
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and not existing.closed:
                if cwd:
                    existing.set_cwd(cwd)
                return existing
            if not create:
                return None
            session = TerminalSession(
                key, cwd=cwd or self.default_cwd or None, shell=shell,
                env=env, events=self.events,
                max_output_chars=(self._max_output_chars
                                  if self._max_output_chars is not None
                                  else self._default("max_output_chars", 20000)),
                history_limit=(self._history_limit
                               if self._history_limit is not None
                               else self._default("history_limit", 50)),
                on_output=self.on_output)
            self._sessions[key] = session
            self._emit("terminal.session_created", session_id=key,
                       cwd=session.cwd, shell=session.shell.get("name"),
                       platform=session.platform)
            return session

    def _default(self, key: str, fallback):
        if self.config is not None:
            try:
                value = self.config.get(f"TERMINAL_{key.upper()}", "")
                if value not in ("", None):
                    return int(value)
            except Exception:
                pass
        return fallback

    def session_ids(self) -> list[str]:
        with self._lock:
            return [sid for sid, s in self._sessions.items() if not s.closed]

    # -- lifecycle -----------------------------------------------------------
    def close(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(str(session_id or DEFAULT_SESSION_ID),
                                         None)
        if session is None:
            return False
        session.close()
        self._emit("terminal.session_closed", session_id=session.session_id)
        return True

    def close_all(self) -> int:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()
        return len(sessions)

    def close_idle(self, max_idle_seconds: float | None = None) -> list[str]:
        limit = self.idle_seconds if max_idle_seconds is None else max_idle_seconds
        if not limit:
            return []
        closed = []
        with self._lock:
            for sid, session in list(self._sessions.items()):
                if session.idle_seconds() >= limit:
                    self._sessions.pop(sid, None)
                    session.close()
                    closed.append(sid)
        for sid in closed:
            self._emit("terminal.session_closed", session_id=sid, reason="idle")
        return closed

    # -- context for the AI --------------------------------------------------
    def context_text(self, session_id: str | None = None, *,
                     max_commands: int = 8, max_chars: int = 2000) -> str:
        session = self.get(session_id, create=False)
        if session is None:
            return ""
        return session.context_text(max_commands=max_commands,
                                    max_chars=max_chars)

    def snapshot(self, session_id: str | None = None) -> dict | None:
        session = self.get(session_id, create=False)
        return session.snapshot() if session is not None else None

    def snapshots(self) -> list[dict]:
        out = []
        for sid in self.session_ids():
            session = self.get(sid, create=False)
            if session is not None:
                out.append(session.snapshot())
        return out

    def _emit(self, kind: str, **data) -> None:
        if self.events is None:
            return
        try:
            self.events.emit(kind, agent="terminal", **data)
        except Exception:
            pass


def default_session_id_for(conversation_id) -> str:
    """Stable, isolated session id for one chat conversation."""
    if conversation_id in (None, "", 0):
        return DEFAULT_SESSION_ID
    return f"conv-{conversation_id}"
