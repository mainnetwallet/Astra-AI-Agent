"""BrowserManager — multiplexed browser sessions + tool surface.

Session isolation: each caller may hold its own `session` id; a fresh id gets
a fresh BrowserSession. The manager farms out the real work to BrowserSession
and shapes the tool-facing contract (args dict -> result dict).
"""
from __future__ import annotations

import threading

from .sessions import BrowserSession, BrowserUnavailableError


class BrowserManager:
    def __init__(self, *, config=None, events=None):
        self.config = config or {}
        self.events = events
        # Sessions belong to THIS manager (not a module-global), so a long
        # test run or a rebuilt app cannot inherit another app's open browser,
        # and close_all() can release every Playwright resource on shutdown.
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return BrowserSession.available

    def status(self) -> dict:
        if not BrowserSession.available:
            return {"available": False,
                    "install_hint": BrowserSession.install_hint()}
        # probe a real session (will only attach this check once)
        try:
            s = BrowserSession(config=self.config)
        except BrowserUnavailableError:
            return {"available": False,
                    "install_hint": BrowserSession.install_hint()}
        st = s.status()
        with self._lock:
            st["sessions"] = list(self._sessions)
        return st

    # -- sessions ------------------------------------------------------------
    def _session(self, session_id: str = "") -> BrowserSession:
        sid = session_id or "default"
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                try:
                    s = BrowserSession(config=self.config)
                except BrowserUnavailableError as exc:
                    raise BrowserUnavailableError(str(exc)) from None
                self._sessions[sid] = s
            return s

    def close_session(self, session_id: str = "") -> dict:
        sid = session_id or "default"
        with self._lock:
            s = self._sessions.pop(sid, None)
        if s:
            s.close()
        return {"status": "ok"}

    def close_all(self) -> None:
        """Close every open session — the app's shutdown path calls this so
        Playwright browsers/contexts are not left running."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass

    # -- tool surface --------------------------------------------------------
    def browser_open(self, args: dict) -> dict:
        s = self._session(args.get("session", ""))
        return s.open(args.get("url", ""))

    def browser_observe(self, args: dict) -> dict:
        s = self._session(args.get("session", ""))
        return s.observe(max_chars=int(args.get("max_chars", 6000)))

    def browser_action(self, args: dict) -> dict:
        s = self._session(args.get("session", ""))
        # 'click' is riskier; browser tools are already gated at the registry
        return s.act(action=args.get("action", ""),
                     selector=args.get("selector", ""),
                     value=args.get("value", ""),
                     index=int(args.get("index", 0)))

    def browser_extract(self, args: dict) -> dict:
        s = self._session(args.get("session", ""))
        return s.extract(selector=args.get("selector", ""),
                         attribute=args.get("attribute", "text"),
                         limit=int(args.get("limit", 50)),
                         as_table=bool(args.get("as_table", False)))

    def browser_screenshot(self, args: dict) -> dict:
        s = self._session(args.get("session", ""))
        return s.screenshot(name=args.get("name", ""))

    def browser_close(self, args: dict) -> dict:
        return self.close_session(args.get("session", ""))
