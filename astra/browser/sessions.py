"""Real Playwright browser session, or an honest "unavailable".

Importing `playwright` here is deferred so that setups without the browser
worker still boot instantly. If Playwright (or its browsers) is missing,
`BrowserSession.available` is False and every method raises
BrowserUnavailableError with a message that tells the operator how to enable
it — the surface never pretends to be ready.
"""
from __future__ import annotations

import os
import threading


class BrowserUnavailableError(RuntimeError):
    """Raised whenever browser automation is attempted but not installed."""


class BrowserSession:
    """One logical browser session: page lifecycle, bounded observation,
    screenshot captures and a WAITING_USER pause for CAPTCHA/MFA walls.

    This is a thin, safe shell. Playwright primitives are imported lazily and
    only touched under a per-session lock.
    """

    available = False
    _checked = False
    _lock = threading.Lock()

    @classmethod
    def _probe(cls) -> bool:
        with cls._lock:
            if cls._checked:
                return cls.available
            cls._checked = True
            try:
                import playwright  # noqa: F401
                cls.available = True
            except Exception:
                cls.available = False
        return cls.available

    @staticmethod
    def install_hint() -> str:
        return ("Browser automation is not installed. Enable it with:\n"
                "    pip install playwright && playwright install chromium\n"
                "or set ASTRA_BROWSER=0 to keep it offline.\n"
                "While unavailable, browser tools are disabled, not simulated.")

    # -- lifecycle -----------------------------------------------------------
    def __init__(self, *, headless: bool = True, screenshots_dir: str = "",
                 config=None):
        if not self._probe():
            raise BrowserUnavailableError(self.install_hint())
        self._lazy = None              # module: playwright.sync_api
        self._pw = None                # playwright instance
        self._browser = None
        self._page = None
        self._context = None
        self._headless = headless
        self._screenshots_dir = screenshots_dir or (
            config.get("BROWSER_SCREENSHOTS", "data/screenshots")
            if config else "data/screenshots")
        os.makedirs(self._screenshots_dir, exist_ok=True)
        self._opened = False
        self._user_lock = False        # paused on CAPTCHA/MFA/login wall

    def _start(self) -> None:
        if self._opened and self._page:
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self.available = False
            raise BrowserUnavailableError(self.install_hint()) from exc
        try:
            self._lazy = sync_playwright
            self._pw = self._lazy().start()
            self._browser = self._pw.chromium.launch(headless=self._headless)
        except Exception as exc:
            self.available = False
            raise BrowserUnavailableError(
                "Playwright is installed but the browser engine failed to "
                "launch. Run `playwright install chromium`.") from exc

    # -- lifecycle helpers ---------------------------------------------------
    def open(self, url: str, *, width: int = 1280, height: int = 800,
             timeout_ms: float = 30_000) -> dict:
        if self._user_lock:
            return self._paused("cannot open a new page while waiting on "
                                "CAPTCHA/MFA — resolve and resume first")
        # Same SSRF guard as URL research: a browser page is untrusted input
        # to the model, so refuse file://, javascript:, loopback and private
        # targets unless the operator explicitly opts in. Without this the
        # browser tool was an unguarded SSRF/local-file path.
        from astra.security import allow_url
        allow_private = os.environ.get("ASTRA_ALLOW_PRIVATE_URLS") == "1"
        if not allow_url(url, allow_private=allow_private):
            return {"status": "error", "url": url,
                    "error": "refused URL (non-http(s) or private/blocked "
                             "network) — set ASTRA_ALLOW_PRIVATE_URLS=1 to "
                             "allow internal targets"}
        self._start()
        if not self._page:
            self._context = self._browser.new_context(
                viewport={"width": width, "height": height},
                user_agent=(self._context_ua()
                            if self._context else None))
            self._page = self._context.new_page()
        self._page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        self._opened = True
        self._check_user_wall(self._page)
        return {"status": "ok", "url": url,
                "title": self._page.title() or "",
                "paused": self._user_lock}

    def _context_ua(self) -> str:
        return ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124 Safari/537.36")

    def _check_user_wall(self, page) -> None:
        """Detect CAPTCHA/MFA/login walls and pause to WAITING_USER."""
        # Biggest, most honest signal: the URL/embedded challenge providers
        # we must never touch. Detection is deliberately conservative.
        try:
            cur = page.url or ""
        except Exception:
            cur = ""
        for nudge in ("recaptcha", "hcaptcha", "geetest", "turnstile",
                      "cloudflare", "challenge", "captcha", "login", "signin",
                      "otp", "2fa", "mfa", "auth"):
            if nudge in cur.lower():
                self._user_lock = True
                return
        try:
            texts = page.locator("body").inner_text(timeout=1500) or ""
        except Exception:
            texts = ""
        low = texts.lower()
        for nudge in ("i'm not a robot", "verify you are human",
                      "enter the code", "two-step verification",
                      "sign in to continue", "enter your password"):
            if nudge in low:
                self._user_lock = True
                return

    def pending_user_action(self) -> bool:
        return self._user_lock

    def _paused(self, reason: str) -> dict:
        return {"status": "waiting_user", "error": reason,
                "paused": True,
                "hint": "Resume continue the session once the human has "
                        "finished the CAPTCHA, MFA or login step."}

    def observe(self, *, max_chars: int = 6000) -> dict:
        """Bounded observation of the current page (never a full-DOM dump).

        Returns a trimmed textual outline: title, url, headings, visible
        text (truncated), interactive controls count, and form field list —
        enough for deterministic follow-up actions without leaking the whole
        page into context.
        """
        if not self._page:
            return {"status": "error", "error": "no page open — open one first"}
        if self._user_lock:
            return self._paused("page is behind a CAPTCHA/MFA/login wall")
        try:
            title = self._page.title() or ""
            url = self._page.url
            text = self._page.locator("body").inner_text(timeout=3000) or ""
            # interactive element census
            interactive = {
                "links": self._page.locator("a").count(),
                "buttons": self._page.locator("button").count(),
                "inputs": self._page.locator("input, textarea, select").count(),
            }
            forms = []
            for el in self._page.locator("input, textarea, select").all()[:12]:
                nm = (el.get_attribute("name") or
                      el.get_attribute("id") or "")[:40]
                typ = el.get_attribute("type") or el.evaluate("e => e.tagName")
                if nm:
                    forms.append({"name": nm, "type": typ or "text"})
            text = "\n".join(
                ln.strip() for ln in text.splitlines() if ln.strip())
            if len(text) > max_chars:
                text = text[:max_chars] + "\n[...truncated]"
            return {"status": "ok", "title": title, "url": url,
                    "text": text, "interactive": interactive, "forms": forms,
                    "paused": False}
        except Exception as exc:
            return {"status": "error", "error": str(exc)[:300],
                    "paused": self._user_lock}

    # -- actions -------------------------------------------------------------
    def act(self, *, action: str, selector: str = "", value: str = "",
            index: int = 0) -> dict:
        """A single, typed browser action (click/fill/scroll/select)."""
        if not self._page:
            return {"status": "error", "error": "no page open — open one first"}
        if self._user_lock:
            return self._paused("page is behind a CAPTCHA/MFA/login wall")
        try:
            action = (action or "").strip().lower()
            if action in ("click", "press", "select", "fill", "type", "clear"):
                if action == "click":
                    if selector:
                        self._page.locator(selector).nth(index).click()
                    else:
                        self._page.mouse.click(20, 20)
                elif action == "fill":
                    self._page.locator(selector).nth(index).fill(value)
                elif action == "type":
                    self._page.locator(selector).nth(index).type(value)
                elif action == "clear":
                    self._page.locator(selector).nth(index).clear()
                elif action == "press":
                    self._page.locator(selector).nth(index).press(value)
                elif action == "select":
                    self._page.locator(selector).nth(index).select_option(value)
                return {"status": "ok", "action": action, "selector": selector,
                        "ok": True}
            elif action in ("scroll", "scroll_down", "scroll_up", "scroll_y"):
                dy = int(value) if value else (800 if "down" in action else -800)
                if "up" in action:
                    dy = -abs(dy)
                self._page.mouse.wheel(0, dy)
                return {"status": "ok", "action": "scroll", "ok": True}
            return {"status": "error", "error": f"unknown action: {action}"}
        except Exception as exc:
            return {"status": "error", "error": str(exc)[:300], "ok": False}

    def extract(self, *, selector: str = "", attribute: str = "text",
                limit: int = 50, as_table: bool = False) -> dict:
        if not self._page:
            return {"status": "error", "error": "no page open — open one first"}
        if self._user_lock:
            return self._paused("page is behind a CAPTCHA/MFA/login wall")
        try:
            if as_table:
                rows = self._page.locator("table tr").all()[:limit]
                out = []
                for r in rows:
                    cells = [c.strip() for c in
                             r.locator("th, td").all_inner_texts()]
                    if cells:
                        out.append(cells)
                return {"status": "ok", "as_table": True, "rows": out}
            if selector:
                els = self._page.locator(selector).all()[:limit]
                values = []
                for el in els:
                    try:
                        values.append(el.inner_text().strip())
                    except Exception:
                        continue
                return {"status": "ok", "selector": selector, "count": len(values),
                        "values": values}
            return self.observe()
        except Exception as exc:
            return {"status": "error", "error": str(exc)[:300], "ok": False}

    def screenshot(self, *, name: str = "") -> dict:
        if not self._page:
            return {"status": "error", "error": "no page open — open one first"}
        import hashlib
        fn = (name or hashlib.sha1((self._page.url or "page").encode()).hexdigest()[:12])
        if not fn.endswith(".png"):
            fn += ".png"
        path = os.path.join(self._screenshots_dir, fn)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            self._page.screenshot(path=path, full_page=False)
            return {"status": "ok", "path": path}
        except Exception as exc:
            return {"status": "error", "error": str(exc)[:300]}

    def close(self) -> dict:
        try:
            for obj in (self._page, self._context, self._browser, self._pw):
                try:
                    if obj is not None:
                        obj.close()
                except Exception:
                    pass
        finally:
            self._page = self._context = self._browser = self._pw = None
            self._opened = False
            self._user_lock = False
        return {"status": "ok"}

    # -- status --------------------------------------------------------------
    def status(self) -> dict:
        return {"available": self.available,
                "open": bool(self._page and self._opened),
                "url": self._page.url if (self._page and self._opened) else "",
                "paused": self._user_lock,
                "screenshots_dir": self._screenshots_dir,
                "install_hint": "" if self.available else self.install_hint()}
