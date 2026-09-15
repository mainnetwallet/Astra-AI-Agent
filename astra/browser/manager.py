"""BrowserManager — multiplexed browser sessions + tool surface.

Session isolation: each caller may hold its own `session` id; a fresh id gets
a fresh BrowserSession. The manager farms out the real work to BrowserSession
and shapes the tool-facing contract (args dict -> result dict).
"""
from __future__ import annotations

from .sessions import BrowserSession, BrowserUnavailableError

_SESSIONS: dict[str, BrowserSession] = {}


class BrowserManager:
    def __init__(self, *, config=None, events=None):
        self.config = config or {}
        self.events = events

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
        st["sessions"] = list(_SESSIONS)
        return st

    # -- sessions ------------------------------------------------------------
    def _session(self, session_id: str = "") -> BrowserSession:
        sid = session_id or "default"
        if sid not in _SESSIONS:
            try:
                _SESSIONS[sid] = BrowserSession(config=self.config)
            except BrowserUnavailableError as exc:
                raise BrowserUnavailableError(str(exc)) from None
        return _SESSIONS[sid]

    def close_session(self, session_id: str = "") -> dict:
        sid = session_id or "default"
        s = _SESSIONS.pop(sid, None)
        if s:
            s.close()
        return {"status": "ok"}

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