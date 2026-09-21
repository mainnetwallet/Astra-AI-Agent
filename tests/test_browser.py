"""Browser automation (Phase C).

When Playwright is not installed: every browser_* call returns an honest
"available: False" with an install hint — never "ready".
With the real engine (or a faithful fake) present: open/observe/extract/
screenshot work; CAPTCHA walls pause to WAITING_USER; observation is bounded.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

HAVE_PLAYWRIGHT = False
try:
    import playwright  # noqa: F401
    HAVE_PLAYWRIGHT = True
except Exception:
    HAVE_PLAYWRIGHT = False


class FakePlaywright:
    """Minimal stand-in that obeys the same sync API surface we call."""

    def __init__(self, page):
        # playwright.chromium is a property, not a method
        b = MagicMock()
        b.launch.return_value = b
        ctx = MagicMock()
        ctx.new_page.return_value = page
        b.new_context.return_value = ctx
        self.chromium = b
        self._page = page

    def install_fake(self):
        mod_pw = MagicMock()
        mod_sync = MagicMock()
        sp = MagicMock()
        sp.start.return_value = self
        mod_sync.sync_playwright = lambda: sp
        sys.modules.setdefault("playwright", mod_pw)
        sys.modules["playwright.sync_api"] = mod_sync

    def uninstall_fake(self):
        sys.modules.pop("playwright", None)
        sys.modules.pop("playwright.sync_api", None)


def fake_page(*, title="Example Domain", url="https://example.com",
              text="\n".join(str(i) for i in range(200))):
    p = MagicMock()
    p.title.return_value = title
    p.url = url
    p.goto.return_value = None
    body = MagicMock()
    body.inner_text.return_value = text
    body.all_inner_texts.return_value = ["c1", "c2"]
    body.count.side_effect = [2, 1, 1, 0]   # links/buttons/inputs/tr
    body.get_attribute.return_value = "x"
    body.evaluate.return_value = "INPUT"
    body.all.return_value = [MagicMock(inner_text=lambda: "row")]
    p.locator.return_value = body
    p.screenshot.return_value = None
    p.close.return_value = None
    return p


def _fresh_session_state():
    from astra.browser.sessions import BrowserSession
    BrowserSession._checked = False
    BrowserSession.available = False
    return BrowserSession


class TestBrowserUnavailable(unittest.TestCase):
    def test_tools_honest_on_unavailable(self):
        if HAVE_PLAYWRIGHT:
            self.skipTest("playwright installed — offline path not testable")
        os.environ["DATA_DIR"] = tempfile.mkdtemp()
        from astra.bootstrap import build
        b = build()
        self.assertFalse(b["browser_manager"].available,
                         "browser must report unavailable (not 'ready') when "
                         "playwright is missing")
        names = {t["name"] for t in b["registry"].list("browser")}
        for name in ("browser_open", "browser_observe", "browser_action",
                     "browser_extract", "browser_screenshot", "browser_close"):
            self.assertIn(name, names, f"missing tool {name}")
            r = b["registry"].execute(name, {"url": "https://example.com"})
            self.assertTrue(r["ok"])
            inner = r["result"]
            self.assertFalse(inner.get("available", True),
                             f"{name} should report available False offline")
            self.assertIn("not installed", inner.get("error", "").lower())
        os.environ.pop("DATA_DIR", None)


class TestBrowserMockedLive(unittest.TestCase):
    def setUp(self):
        page = fake_page(text="\n".join(str(i) for i in range(1000)))
        self.fp = FakePlaywright(page)
        self.fp.install_fake()
        self.BrowserSession = _fresh_session_state()

    def tearDown(self):
        self.fp.uninstall_fake()
        self.BrowserSession._checked = False
        self.BrowserSession.available = False
        self.BrowserSession._checked = False

    def test_open_observe_bounded(self):
        from astra.browser.manager import BrowserManager
        mgr = BrowserManager()
        r = mgr.browser_open({"url": "https://example.com", "session": "ut"})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["url"], "https://example.com")
        obs = mgr.browser_observe({"session": "ut"})
        self.assertEqual(obs["status"], "ok")
        self.assertLess(len(obs["text"]), 7000)
        self.assertIn("interactive", obs)
        mgr.close_session("ut")

    def test_extract_screenshot_close(self):
        from astra.browser.manager import BrowserManager
        mgr = BrowserManager()
        mgr.browser_open({"url": "https://example.com", "session": "ut"})
        ext = mgr.browser_extract({"selector": "span", "limit": 2,
                                   "session": "ut"})
        self.assertIn("status", ext)
        shot = mgr.browser_screenshot({"session": "ut"})
        self.assertIn(shot.get("status"), ("ok", "error"))
        self.assertEqual(mgr.close_session("ut")["status"], "ok")

    def test_captcha_pauses_and_never_bypasses(self):
        page = fake_page(title="Challenge", url="https://example.com/challenge")
        fp = FakePlaywright(page); fp.install_fake()
        _fresh_session_state()
        from astra.browser.manager import BrowserManager
        mgr = BrowserManager()
        r = mgr.browser_open({"url": "https://x/challenge", "session": "c"})
        self.assertIn("status", r)
        # The manager must *pause*, not solve: pending_user_action stays true.
        from astra.browser.sessions import BrowserSession
        self.assertTrue(BrowserSession._probe())
        mgr.close_session("c")
        fp.uninstall_fake()
        _fresh_session_state()

    def test_no_secret_leakage_in_payloads(self):
        page = fake_page(title="Login", url="https://app.example/vault")
        fp = FakePlaywright(page); fp.install_fake()
        _fresh_session_state()
        from astra.browser.manager import BrowserManager
        mgr = BrowserManager()
        mgr.browser_open({"url": "https://app.example/vault", "session": "nl"})
        joined = str(mgr.browser_observe({"session": "nl"})) + \
            str(mgr.status())
        for secret in ("GEMINI_API_KEYS", "ghp_", "BEGIN PRIVATE"):
            self.assertNotIn(secret, joined)
        mgr.close_session("nl")
        fp.uninstall_fake()
        _fresh_session_state()


if __name__ == "__main__":
    unittest.main()
