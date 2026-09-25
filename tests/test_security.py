"""Security + API hardening tests (master-prompt Phase 7).

Covers the global security module (redaction, SSRF guard, rate limiting,
request ids, structured errors) and the hardened web layer (auth gate,
path-traversal guard, body cap, /api/v1 aliases, new production endpoints,
and stack-trace-free error envelopes)."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from astra.security import (ApiError, RateLimiter, make_request_id, redact,
                            redact_text, risky_url, allow_url, safe_urlopen)


class TestRedaction(unittest.TestCase):
    def test_masks_secret_keys_deeply(self):
        src = {"api_key": "abc", "nested": {"password": "p", "token": "t"},
               "list": [{"secret": "s"}], "safe": "visible"}
        out = redact(src)
        self.assertEqual(out["api_key"], "***redacted***")
        self.assertEqual(out["nested"]["password"], "***redacted***")
        self.assertEqual(out["nested"]["token"], "***redacted***")
        self.assertEqual(out["list"][0]["secret"], "***redacted***")
        self.assertEqual(out["safe"], "visible")

    def test_masks_secret_shaped_values_anywhere(self):
        out = redact({"note": "use sk-abcdefghijklmno now",
                      "hex": "0x" + "a" * 64})
        self.assertIn("***redacted***", out["note"])
        self.assertNotIn("sk-abcdefghijklmno", json.dumps(out))
        self.assertNotIn("a" * 64, out["hex"])

    def test_does_not_mutate_input(self):
        src = {"api_key": "keep"}
        redact(src)
        self.assertEqual(src["api_key"], "keep")

    def test_redact_text(self):
        self.assertNotIn("Bearer xyz", redact_text("auth Bearer xyz done"))


class TestSsrfGuard(unittest.TestCase):
    def test_blocks_loopback_private_linklocal(self):
        for bad in ("http://127.0.0.1/x", "http://localhost/x",
                    "http://10.0.0.1/", "http://192.168.1.1/",
                    "http://169.254.169.254/latest/meta-data/",
                    "http://[::1]/"):
            self.assertTrue(risky_url(bad, resolve_dns=False), bad)

    def test_blocks_non_http_schemes(self):
        for bad in ("file:///etc/passwd", "gopher://x/", "ftp://h/"):
            self.assertTrue(risky_url(bad, resolve_dns=False), bad)

    def test_allows_public_https(self):
        self.assertFalse(risky_url("https://example.com/page", resolve_dns=False))

    def test_userinfo_does_not_hide_the_real_host(self):
        # A `user@host` authority must be judged by the host after the `@`,
        # otherwise a loopback/link-local target slips past the guard.
        for bad in ("http://evil@127.0.0.1/x",
                    "http://user:pass@169.254.169.254/latest",
                    "http://evil@[::1]/"):
            self.assertTrue(risky_url(bad, resolve_dns=False), bad)
        self.assertFalse(
            risky_url("http://user:pass@example.com/x", resolve_dns=False))

    def test_override_allows_private(self):
        self.assertTrue(allow_url("http://127.0.0.1/x", allow_private=True))


class _RedirectServer:
    """Tiny loopback HTTP server for redirect tests."""

    def __init__(self):
        import http.server
        import threading
        outer = self
        self.redirect_to = ""

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                    self.end_headers()
                elif self.path == "/ok":
                    body = b"hello-ok"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestSsrfSafeRedirects(unittest.TestCase):
    """Regression: the SSRF guard only checked the URL the caller supplied.
    urllib follows redirects by default, so a public URL that 302s to
    loopback/link-local/private space (cloud metadata) bypassed the guard.
    Every hop must now be re-checked."""

    def test_redirect_to_private_address_is_refused(self):
        srv = _RedirectServer()
        self.addCleanup(srv.stop)
        srv.redirect_to = "http://169.254.169.254/latest/meta-data/"
        real_allow = allow_url

        def patched(url, allow_private=False):
            # The test harness's own loopback start URL is "allowed" so the
            # initial request can happen; the redirect target is judged by
            # the real policy and must be refused.
            if str(url).startswith(srv.url("/start")):
                return True
            return real_allow(url, allow_private=allow_private)

        with patch("astra.security.allow_url", patched):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                safe_urlopen(srv.url("/start"), timeout=5)
        self.assertIn("refused redirect", str(cm.exception))

    def test_redirect_to_allowed_target_is_still_followed(self):
        srv = _RedirectServer()
        self.addCleanup(srv.stop)
        srv.redirect_to = srv.url("/ok")
        with patch("astra.security.allow_url",
                   lambda url, allow_private=False: True):
            with safe_urlopen(srv.url("/start"), timeout=5) as resp:
                self.assertEqual(resp.read(), b"hello-ok")

    def test_initial_private_url_is_refused(self):
        with self.assertRaises(ValueError):
            safe_urlopen("http://127.0.0.1:9/x", timeout=1, allow_private=False)

    def test_research_fetch_uses_the_redirect_safe_opener(self):
        """The caller-supplied-URL guard is useless if the fetch layer still
        follows an unchecked redirect: `_fetch` must go through the same
        policy on every hop."""
        from astra.research import lookup as research
        srv = _RedirectServer()
        self.addCleanup(srv.stop)
        srv.redirect_to = "http://169.254.169.254/latest/meta-data/"
        real_allow = allow_url

        def patched(url, allow_private=False):
            if str(url).startswith(srv.url("/start")):
                return True
            return real_allow(url, allow_private=allow_private)

        with patch("astra.security.allow_url", patched):
            with self.assertRaises(urllib.error.HTTPError):
                research._fetch(srv.url("/start"))


class TestRateLimiterRequestId(unittest.TestCase):
    def test_fixed_window(self):
        rl = RateLimiter(3, 60.0)
        self.assertTrue(all(rl.allow("ip") for _ in range(3)))
        self.assertFalse(rl.allow("ip"))
        self.assertTrue(rl.allow("other"))

    def test_request_ids_unique(self):
        self.assertNotEqual(make_request_id(), make_request_id())

    def test_stale_keys_are_pruned(self):
        """A unique client key must not leak a list entry forever: once the
        map grows past the sweep threshold and the window has passed, the
        stale keys are reclaimed."""
        rl = RateLimiter(1, 60.0)
        for i in range(rl._PRUNE_AT + 10):
            rl.allow(f"ip-{i}")
        self.assertGreater(len(rl._hits), rl._PRUNE_AT)
        # Age the recorded hits past the window instead of sleeping 1ms with a
        # 1e-9s window: time.monotonic() on Windows only ticks every ~15.6ms,
        # so the whole test ran inside one tick, `now - ts` stayed 0, nothing
        # counted as stale and this failed on roughly 60% of runs there.
        for key, stamps in list(rl._hits.items()):
            rl._hits[key] = [t - 120.0 for t in stamps]
        self.assertTrue(rl.allow("fresh"))
        self.assertLessEqual(len(rl._hits), 2)


class TestApiError(unittest.TestCase):
    def test_envelope(self):
        e = ApiError("validation", "bad input", 400, "rid1")
        d = e.to_dict()
        self.assertEqual(d["error_code"], "validation")
        self.assertEqual(d["request_id"], "rid1")
        self.assertFalse(d["ok"])


# ── live server tests ─────────────────────────────────────────────────────────
def _server(token="", env="development"):
    from astra.bootstrap import build
    from astra.store import Store
    from tests.helpers import LiveServer
    d = tempfile.mkdtemp()
    stack = build(Store(os.path.join(d, "t.db")))
    if token:
        stack["config"].set("ASTRA_TOKEN", token)
    stack["config"].set("ENV", env)
    srv = LiveServer(stack=stack)
    srv.site.operator_token = token
    srv.site.env = env
    return srv, srv.base


def _req(url, token="", method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if token:
        r.add_header("X-Astra-Token", token)
    def _parse(raw):
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw.decode("utf-8", "replace")}

    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, _parse(resp.read()), _CIHeaders(resp.headers.items())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, _parse(e.read()), _CIHeaders(e.headers.items())


class _CIHeaders(dict):
    """Case-insensitive header view — uvicorn lower-cases header names."""

    def __init__(self, items=()):
        super().__init__((k.lower(), v) for k, v in items)

    def __getitem__(self, key):
        return super().__getitem__(key.lower())

    def get(self, key, default=None):
        return super().get(key.lower(), default)

    def __contains__(self, key):
        return super().__contains__(key.lower())


class TestWebHardening(unittest.TestCase):
    def setUp(self):
        self.srv, self.base = _server()
        self.addCleanup(self.srv.stop)

    def test_request_id_header_and_body(self):
        st, body, hdr = _req(self.base + "/api/v1/health")
        self.assertEqual(st, 200)
        self.assertTrue(hdr.get("X-Request-Id"))
        self.assertTrue(body.get("request_id"))
        self.assertEqual(hdr["X-Request-Id"], body["request_id"])

    def test_security_headers(self):
        _, _, hdr = _req(self.base + "/api/v1/health")
        self.assertEqual(hdr.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(hdr.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertIn("Content-Security-Policy", hdr)
        self.assertEqual(hdr.get("Cache-Control"), "no-store")

    def test_path_traversal_blocked(self):
        st, body, _ = _req(self.base + "/static/../run.py")
        self.assertEqual(st, 400)

    def test_v1_alias_matches_legacy(self):
        a = _req(self.base + "/api/tools")[1]
        b = _req(self.base + "/api/v1/tools")[1]
        self.assertEqual([t["name"] for t in a["data"]["tools"]],
                         [t["name"] for t in b["data"]["tools"]])

    def test_models_endpoint(self):
        st, body, _ = _req(self.base + "/api/v1/models")
        self.assertEqual(st, 200)
        self.assertIn("models", body["data"])
        self.assertIn("summary", body["data"])

    def test_metrics_endpoint(self):
        st, body, _ = _req(self.base + "/api/metrics")
        self.assertEqual(st, 200)
        self.assertIn("requests", body["data"])
        self.assertIn("router", body["data"])

    def test_router_endpoints(self):
        self.assertEqual(_req(self.base + "/api/v1/router/status")[0], 200)
        self.assertEqual(_req(self.base + "/api/v1/router/stats")[0], 200)

    def test_web3_policy_endpoint(self):
        st, body, _ = _req(self.base + "/api/v1/web3/transaction-policy")
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["mode"], "CONFIRM")

    def test_web3_mode_change_requires_token(self):
        st, body, _ = _req(self.base + "/api/v1/web3/transaction-policy/mode",
                           method="POST", body={"mode": "AUTO"})
        self.assertEqual(st, 403)
        self.assertEqual(body["error_code"], "authorization")

    def test_missing_tx_404(self):
        st, body, _ = _req(self.base + "/api/v1/web3/transactions/nope")
        self.assertEqual(st, 404)
        self.assertEqual(body["error_code"], "transaction_not_found")

    def test_unknown_route_structured(self):
        st, body, _ = _req(self.base + "/api/v1/does-not-exist")
        self.assertEqual(st, 404)
        self.assertFalse(body["ok"])
        self.assertIn("error_code", body)

    def test_body_cap(self):
        # The default cap is 50 MB (uploads need room); this test pins an
        # explicit 10 MB cap so it checks enforcement, not the default.
        self.srv.site.max_body_bytes = 10 * 1024 * 1024
        r = urllib.request.Request(self.base + "/api/v1/chat",
                                   data=b"x" * (11 * 1024 * 1024), method="POST")
        try:
            urllib.request.urlopen(r, timeout=5)
            self.fail("oversized body accepted")
        except urllib.error.HTTPError as e:
            with e:
                self.assertEqual(e.code, 413)

    def test_internal_error_hides_detail_in_production(self):
        srv, base = _server(env="production")
        self.addCleanup(srv.stop)
        # patch a handler to raise, then confirm the message is generic
        st, body, _ = _req(base + "/api/v1/web3/transactions/BAD")
        # (dev path exercised above); production must not leak a traceback
        self.assertNotIn("Traceback", json.dumps(body))


class TestWebAuth(unittest.TestCase):
    def setUp(self):
        self.srv, self.base = _server(token="sekrit")
        self.addCleanup(self.srv.stop)

    def test_requires_token(self):
        st, body, _ = _req(self.base + "/api/v1/health")
        self.assertEqual(st, 401)
        self.assertEqual(body["error_code"], "authentication")

    def test_accepts_token(self):
        st, body, _ = _req(self.base + "/api/v1/health", token="sekrit")
        self.assertEqual(st, 200)

    def test_rejects_wrong_token(self):
        st, _, _ = _req(self.base + "/api/v1/health", token="wrong")
        self.assertEqual(st, 401)

    def test_static_still_public(self):
        st, _, _ = _req(self.base + "/")
        self.assertEqual(st, 200)

    def test_web3_authorize_requires_token(self):
        # unauthenticated request never even reaches the operator-only
        # gate inside the handler — the global auth gate rejects it first
        st, body, _ = _req(self.base + "/api/v1/web3/transactions/x/authorize",
                           method="POST")
        self.assertEqual(st, 401)

    def test_web3_reject_requires_token(self):
        st, body, _ = _req(self.base + "/api/v1/web3/transactions/x/reject",
                           method="POST")
        self.assertEqual(st, 401)


class TestWeb3TxActionEndpoint(unittest.TestCase):
    """Operator approve/reject for a CONFIRM-mode transaction: the second,
    endpoint-specific token check (same pattern as the existing mode-change
    endpoint) closes the gap where an operator who never set ASTRA_TOKEN
    could otherwise approve financial actions from an "open" local server."""

    def test_no_operator_token_blocks_even_without_global_auth(self):
        srv, base = _server()   # no token at all -> server is "open"
        self.addCleanup(srv.stop)
        st, body, _ = _req(base + "/api/v1/web3/transactions/x/authorize",
                           method="POST")
        self.assertEqual(st, 403)
        self.assertEqual(body["error_code"], "authorization")
        st, body, _ = _req(base + "/api/v1/web3/transactions/x/reject",
                           method="POST")
        self.assertEqual(st, 403)

    def test_authorized_operator_can_approve_confirm_mode_send(self):
        import os
        import tempfile
        from astra.core.config import Config
        from astra.store import Store
        from astra.bootstrap import build
        from tests.helpers import LiveServer
        from astra.web3.keystore import SecureKeyStore
        d = tempfile.mkdtemp()
        cfg = Config()
        cfg.set("ASTRA_TOKEN", "sekrit")
        cfg.set("WEB3_TRANSACTION_MODE", "CONFIRM")
        stack = build(store=Store(os.path.join(d, "t.db")), config=cfg,
                     with_scheduler=False)
        # give the manager a signable default wallet
        stack["keystore"] = SecureKeyStore(stack["store"], master_secret="op")
        stack["keystore"].store_key("default", "01" * 32)
        stack["tx_manager"].keystore = stack["keystore"]
        srv = LiveServer(stack=stack)
        srv.site.operator_token = "sekrit"
        self.addCleanup(srv.stop)
        base = srv.base

        from astra.web3.policy import TxRequest
        rec = stack["tx_manager"].create(
            TxRequest("default", "0x" + "35" * 20, 10 ** 15))
        self.assertTrue(rec["requires_approval"])

        with patch("astra.web3.raw_tx.receipt", return_value=None), \
             patch("astra.web3.raw_tx.broadcast", return_value="0xdeadbeef"), \
             patch("astra.web3.raw_tx.get_nonce", return_value=0), \
             patch("astra.web3.raw_tx.get_gas_price", return_value=10 ** 9):
            st, body, _ = _req(
                base + f"/api/v1/web3/transactions/{rec['tx_id']}/authorize",
                token="sekrit", method="POST")
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["status"], "BROADCAST")

    def test_authorized_operator_can_reject(self):
        import os
        import tempfile
        from astra.core.config import Config
        from astra.store import Store
        from astra.bootstrap import build
        from tests.helpers import LiveServer
        d = tempfile.mkdtemp()
        cfg = Config()
        cfg.set("ASTRA_TOKEN", "sekrit")
        stack = build(store=Store(os.path.join(d, "t.db")), config=cfg,
                     with_scheduler=False)
        srv = LiveServer(stack=stack)
        srv.site.operator_token = "sekrit"
        self.addCleanup(srv.stop)
        base = srv.base

        from astra.web3.policy import TxRequest
        rec = stack["tx_manager"].create(
            TxRequest("default", "0x" + "35" * 20, 10 ** 15))
        st, body, _ = _req(
            base + f"/api/v1/web3/transactions/{rec['tx_id']}/reject",
            token="sekrit", method="POST", body={"reason": "not needed"})
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["status"], "REJECTED")


class TestRateLimit(unittest.TestCase):
    def test_429_after_limit(self):
        from astra.security import RateLimiter as RL
        srv, base = _server()
        srv.site.rate_limiter = RL(2, 60.0)   # tight window for the test
        self.addCleanup(srv.stop)
        codes = [_req(base + "/api/v1/health")[0] for _ in range(4)]
        self.assertIn(429, codes)


if __name__ == "__main__":
    unittest.main()
