"""Browser automation for Astra.

Real Playwright-based browsing with a graceful offline fallback: when
Playwright is not installed the BrowserManager reports itself "unavailable"
and every browser_* tool returns a clear error — never a fake "ready".

Design rules (spec):
- Context observation is bounded (see observe()); we never dump a full DOM.
- CAPTCHA / MFA / login walls pause the session to WAITING_USER — the
  manager never attempts to bypass them.
- Screenshots are saved to disk and referenced by path; secrets never enter
  logs or event payloads.
"""
from __future__ import annotations

from .sessions import BrowserSession, BrowserUnavailableError
from .manager import BrowserManager

__all__ = ["BrowserSession", "BrowserUnavailableError", "BrowserManager"]


def register_browser_tools(reg, manager=None) -> int:
    """Register the six browser_* tools (gated beyond read level).

    Returns the number of tools registered. `manager` defaults to a fresh
    BrowserManager. Browser tools are registered regardless of Playwright
    availability — each call fails honestly with the install hint when the
    engine is missing, so planners keep seeing the verbs they plan against.
    """
    from astra.tools.registry import ToolRegistry  # noqa: F401
    from astra.tools.schemas import Tool, Level

    manager = manager or BrowserManager()

    def _wrap(method):
        def fn(args: dict, ctx=None) -> dict:
            if not BrowserSession.available:
                return {"status": "error",
                        "error": BrowserSession.install_hint(),
                        "available": False}
            try:
                return method(args)
            except BrowserUnavailableError as exc:
                return {"status": "error", "error": str(exc),
                        "available": False}
            except Exception as exc:  # pragma: no cover - defensive
                return {"status": "error", "error": str(exc)[:300],
                        "available": True}
        fn.__name__ = method.__name__
        return fn

    specs = [
        # name, method, description, risk, confirmation
        ("browser_open", "browser_open",
         "Open a URL in a real browser session (returns title + url).",
         Level.BROWSER_ACTION, False),
        ("browser_observe", "browser_observe",
         "Bounded textual observation of the current page (title, href, "
         "visible text, controls).",
         Level.READ, False),
        ("browser_action", "browser_action",
         "Perform one typed action: click/fill/type/clear/press/select/scroll.",
         Level.BROWSER_ACTION, False),
        ("browser_extract", "browser_extract",
         "Extract text, attribute values, or table rows from selectors.",
         Level.READ, False),
        ("browser_screenshot", "browser_screenshot",
         "Save a screenshot of the current page and return its file path.",
         Level.BROWSER_ACTION, False),
        ("browser_close", "browser_close",
         "Close a browser session and release its resources.",
         Level.LOW_RISK_WRITE, False),
    ]
    n = 0
    for name, method, description, risk, conf in specs:
        reg.register(Tool(
            name=name,
            fn=_wrap(getattr(manager, method)),
            description=description, category="browser", risk=risk,
            requires_confirmation=conf,
            input={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                    "action": {"type": "string", "enum": ["click", "fill",
                                                          "type", "clear",
                                                          "press", "select",
                                                          "scroll", "scroll_down",
                                                          "scroll_up"]},
                    "session": {"type": "string"},
                    "max_chars": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "attribute": {"type": "string"},
                    "as_table": {"type": "boolean"},
                    "name": {"type": "string"},
                    "index": {"type": "integer"},
                },
            },
            timeout=60.0, retries=1, retry_backoff_s=1.0,
            idempotent=name in ("browser_open", "browser_observe",
                                "browser_extract", "browser_close"),
            strict=False, plugin="core"))
        n += 1
    return n