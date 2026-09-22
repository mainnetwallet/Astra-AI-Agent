"""Tests for the FastAPI/ASGI web layer — the only server Astra ships.

There are two levels here:

  * `WebCoreTests` drives the neutral router in `astra.web` with no socket at
    all — auth, traversal, body cap, streaming and the JSON envelope are
    properties of the router, not of the HTTP stack.
  * `LiveApiTests` boots the real FastAPI/uvicorn app on an ephemeral port and
    checks what only a live server can: headers, statuses, static bytes, SSE
    framing, CORS preflight.

The lifespan and worker-pool tests then pin the two ASGI-specific contracts:
who owns the stack, and that an idle SSE feed never holds a worker thread.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from astra.web import AstraSite, Request, WebApp

from tests.helpers import LiveServer, make_stack


def _stack():
    return make_stack()


# ── core-only tests (no server needed) ──────────────────────────────────────
class WebCoreTests(unittest.TestCase):
    """The neutral router, exercised with no socket at all."""

    def setUp(self):
        self.stack = _stack()
        self.site = AstraSite(("127.0.0.1", 0), self.stack["store"],
                              self.stack["agent"], stack=self.stack)
        self.app = WebApp(self.site)

    def tearDown(self):
        self.stack["store"].close()

    def _call(self, method, path, body=None):
        return self.app.handle(Request(method, path, body=body or {}))

    def _payload(self, resp):
        return json.loads(resp.body.decode("utf-8"))

    def test_health_ok_and_request_id(self):
        resp = self._call("GET", "/api/health")
        self.assertEqual(resp.status, 200)
        data = self._payload(resp)
        self.assertTrue(data["ok"])
        self.assertTrue(data["request_id"])

    def test_v1_alias_hits_the_same_route(self):
        a = self._payload(self._call("GET", "/api/tools"))
        b = self._payload(self._call("GET", "/api/v1/tools"))
        self.assertEqual([t["name"] for t in a["data"]["tools"]],
                         [t["name"] for t in b["data"]["tools"]])

    def test_unknown_route_is_structured_404(self):
        resp = self._call("GET", "/api/v1/does-not-exist")
        self.assertEqual(resp.status, 404)
        data = self._payload(resp)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error_code"], "bad_request")

    def test_non_integer_query_params_are_400_not_500(self):
        # `int()` on raw query input used to raise ValueError and surface as
        # an opaque 500. A malformed client parameter is a 400.
        for path in ("/api/chat/history?after_id=abc",
                     "/api/chat/history?limit=xyz",
                     "/api/chat/history?conversation_id=nope",
                     "/api/events?limit=abc",
                     "/api/events?after_id=x",
                     "/api/memory?limit=x",
                     "/api/memory/search?k=x"):
            resp = self._call("GET", path)
            self.assertEqual(resp.status, 400, path)
            self.assertEqual(self._payload(resp)["error_code"], "bad_request", path)

    def test_non_integer_path_and_body_params_are_400(self):
        for method, path in (("POST", "/api/workflows/abc/run"),
                             ("DELETE", "/api/schedules/abc")):
            resp = self._call(method, path, body={})
            self.assertEqual(resp.status, 400, path)
        resp = self._call("POST", "/api/tasks",
                          body={"goal": "g", "priority": "high"})
        self.assertEqual(resp.status, 400)

    def test_uploads_dir_follows_data_dir(self):
        from astra.web import uploads_dir

        class _Cfg:
            def get(self, key, default=None):
                return "/srv/astra" if key == "DATA_DIR" else default

        self.assertEqual(uploads_dir(_Cfg()), os.path.join("/srv/astra",
                                                           "uploads"))

    def test_path_traversal_blocked(self):
        resp = self._call("GET", "/static/../run.py")
        self.assertEqual(resp.status, 400)

    def test_encoded_traversal_out_of_uploads_is_refused(self):
        """Regression guard for the stored-file routes: a percent-decoded
        segment can be a single "../.." (split_path decodes AFTER splitting
        on "/"), so the resolved path must be containment-checked against the
        uploads root, never merely `startswith`-ed against a prefix."""
        for path in ("/api/uploads/..%2f..%2frun.py",
                     "/api/uploads/%2e%2e%2f%2e%2e%2frun.py",
                     "/api/artifacts/x/..%2f..%2frun.py"):
            resp = self._call("GET", path)
            self.assertIn(resp.status, (403, 404), path)
            self.assertNotIn(b"import", resp.body or b"", path)

    def test_auth_gate_rejects_without_token(self):
        self.site.operator_token = "sekrit"
        resp = self._call("GET", "/api/health")
        self.assertEqual(resp.status, 401)
        self.assertEqual(self._payload(resp)["error_code"], "authentication")

    def test_auth_gate_accepts_query_token(self):
        self.site.operator_token = "sekrit"
        resp = self._call("GET", "/api/health?token=sekrit")
        self.assertEqual(resp.status, 200)

    def test_static_stays_public_with_token_set(self):
        self.site.operator_token = "sekrit"
        self.assertEqual(self._call("GET", "/").status, 200)

    def test_oversized_body_is_413(self):
        from astra.web import check_body_size
        self.site.max_body_bytes = 1024
        self.assertIsNotNone(check_body_size(self.site, 4096))
        self.assertIsNone(check_body_size(self.site, 10))

    def test_stream_response_has_no_content_length(self):
        from astra.web import response_headers, sse_frames
        import inspect
        # the feed is an async generator, so the ASGI server never has to
        # drive it inside a worker thread
        self.assertTrue(inspect.isasyncgenfunction(sse_frames))
        resp = self.app.handle(Request("GET", "/api/events/stream"))
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.has_stream)
        self.assertIsNotNone(resp.stream)
        names = [n.lower() for n, _ in
                 response_headers(resp, Request("GET", "/"), self.site)]
        self.assertNotIn("content-length", names)


# ── live server tests ───────────────────────────────────────────────────────
def _request(port, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None and not (headers or {}).get("Content-Type"):
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read(), dict(e.headers)


def _hdr(headers, name, default=None):
    """Case-insensitive header lookup (uvicorn lower-cases header names)."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return default


SECURITY_HEADERS = ("X-Content-Type-Options", "X-Frame-Options",
                    "Content-Security-Policy", "Referrer-Policy",
                    "Cross-Origin-Opener-Policy")


class LiveApiTests(unittest.TestCase):
    """The real FastAPI/uvicorn server on an ephemeral port."""

    def setUp(self):
        self.stack = _stack()
        self.srv = LiveServer(stack=self.stack)
        self.addCleanup(self.stack["store"].close)

    def tearDown(self):
        self.srv.stop()

    def _get(self, path, **kw):
        return _request(self.srv.port, "GET", path, **kw)

    def _assert_hardened(self, status, body, headers):
        for name in SECURITY_HEADERS:
            self.assertIsNotNone(_hdr(headers, name), f"{name} missing")
        self.assertTrue(_hdr(headers, "X-Request-Id"))
        if "application/json" in _hdr(headers, "Content-Type", ""):
            self.assertTrue(json.loads(body)["request_id"])

    def test_health(self):
        status, body, headers = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self._assert_hardened(status, body, headers)

    def test_manifest_and_tools(self):
        self.assertEqual(self._get("/api/manifest")[0], 200)
        status, body, _ = self._get("/api/tools")
        self.assertGreaterEqual(len(json.loads(body)["data"]["tools"]), 13)

    def test_models_and_router_status(self):
        self.assertEqual(self._get("/api/v1/models")[0], 200)
        self.assertEqual(self._get("/api/v1/router/status")[0], 200)

    def test_static_index_and_asset(self):
        status, body, headers = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", _hdr(headers, "Content-Type", ""))
        self.assertTrue(body)
        self.assertEqual(self._get("/static/js/astra.js")[0], 200)

    def test_unknown_route(self):
        status, body, headers = self._get("/api/v1/does-not-exist")
        self.assertEqual(status, 404)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "bad_request")

    def test_traversal(self):
        self.assertEqual(self._get("/static/../run.py")[0], 400)

    def test_favicon_is_204(self):
        status, _, headers = self._get("/favicon.ico")
        self.assertEqual(status, 204)
        self.assertTrue(_hdr(headers, "X-Request-Id"))
        # RFC 9110: a 204 must not carry Content-Length
        self.assertIsNone(_hdr(headers, "Content-Length"))

    def test_options_preflight(self):
        status, _, headers = _request(
            self.srv.port, "OPTIONS", "/api/health",
            headers={"Origin": "http://localhost:8787"})
        self.assertEqual(status, 204)
        self.assertEqual(_hdr(headers, "Access-Control-Allow-Origin"),
                         "http://localhost:8787")
        self.assertEqual(_hdr(headers, "Access-Control-Allow-Methods"),
                         "GET,POST,PATCH,DELETE,OPTIONS")
        self.assertIsNone(_hdr(headers, "Content-Length"))

    def test_memory_round_trip(self):
        _request(self.srv.port, "POST", "/api/memory",
                 {"content": "live memo", "category": "note"})
        status, body, _ = self._get("/api/memory/search?query=live&k=1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"][0]["content"], "live memo")

    def test_chat_turn(self):
        class _StubAgent:
            def handle(self, msg, context="", history=None, attachments=None):
                return {"ok": True, "reply": f"echo: {msg}",
                        "action": None, "data": {}}

        self.srv.site.agent = _StubAgent()
        status, body, _ = _request(self.srv.port, "POST", "/api/chat",
                                   {"message": "hi"})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["reply"], "echo: hi")

    def test_sse_stream_frames(self):
        # A frame must exist and after_id=0 must be used, otherwise the feed
        # (correctly) idles without writing anything.
        self.stack["events"].emit("ai.completed", agent="live")
        req = urllib.request.Request(
            f"{self.srv.base}/api/events/stream?after_id=0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream",
                          _hdr(dict(resp.headers), "Content-Type", ""))
            frame = resp.read(1)
        self.assertTrue(frame)

    def test_auth_token_gate(self):
        self.srv.site.operator_token = "sekrit"
        try:
            status, body, _ = self._get("/api/health")
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body)["error_code"], "authentication")
            # X-Astra-Token and ?token= both work; static stays public
            self.assertEqual(
                _request(self.srv.port, "GET", "/api/health",
                         headers={"X-Astra-Token": "sekrit"})[0], 200)
            self.assertEqual(self._get("/")[0], 200)
        finally:
            self.srv.site.operator_token = ""


class _FakeScheduler:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


async def _drive_lifespan(app):
    """Run an ASGI app's lifespan startup+shutdown without uvicorn."""
    async with app.router.lifespan_context(app):
        pass


class LifespanTests(unittest.TestCase):
    """Who owns the stack is decided by what `make_app` was handed."""

    def test_own_stack_is_built_and_closed(self):
        from astra.web_fastapi import make_app
        stack = _stack()
        with mock.patch("astra.bootstrap.build", return_value=stack) as built:
            app = make_app()          # nothing handed over -> app owns it
            asyncio.run(_drive_lifespan(app))
        built.assert_called_once()
        # shutdown closed the store it created
        with self.assertRaises(Exception):
            stack["store"].fetch("SELECT 1")

    def test_supplied_stack_is_closed_on_shutdown(self):
        from astra.web_fastapi import make_app
        stack = _stack()
        app = make_app(stack=stack)
        asyncio.run(_drive_lifespan(app))
        with self.assertRaises(Exception):
            stack["store"].fetch("SELECT 1")

    def test_scheduler_is_stopped_on_shutdown(self):
        from astra.web_fastapi import make_app
        stack = _stack()
        sched = _FakeScheduler()
        stack["scheduler"] = sched
        app = make_app(stack=stack)
        asyncio.run(_drive_lifespan(app))
        self.assertTrue(sched.stopped)

    def test_caller_owned_site_is_left_alone(self):
        from astra.web_fastapi import make_app
        stack = _stack()
        site = AstraSite(("127.0.0.1", 0), stack["store"], stack["agent"],
                         stack=stack)
        app = make_app(site=site)
        asyncio.run(_drive_lifespan(app))
        # the caller still owns it: the store must still answer
        self.assertEqual(
            stack["store"].fetchone("SELECT COUNT(*) c FROM sqlite_master")["c"] > 0,
            True)
        stack["store"].close()

    def test_lifecycle_can_be_overridden(self):
        from astra.web_fastapi import make_app
        stack = _stack()
        app = make_app(stack=stack, manage_lifecycle=False)
        asyncio.run(_drive_lifespan(app))
        self.assertEqual(
            stack["store"].fetchone("SELECT COUNT(*) c FROM sqlite_master")["c"] > 0,
            True)
        stack["store"].close()

    def test_create_app_is_a_zero_arg_factory(self):
        from astra.web_fastapi import create_app
        stack = _stack()
        with mock.patch("astra.bootstrap.build", return_value=stack):
            app = create_app()
            asyncio.run(_drive_lifespan(app))
        # an ASGI app object with exactly the two things uvicorn needs
        self.assertTrue(callable(app))
        self.assertTrue(hasattr(app, "router"))


class WorkerPoolTests(unittest.TestCase):
    """An idle SSE tab must not consume a worker thread.

    The shared pool is set to a single thread for this test, so if the live
    feed held a slot while idle, the ordinary request below could never be
    served. With the async drive it is served immediately.
    """

    def setUp(self):
        self.stack = _stack()
        self._env = os.environ.get("ASTRA_ASGI_THREADS")
        os.environ["ASTRA_ASGI_THREADS"] = "1"
        # the limit is applied on ASGI startup, so the env var must be set
        # before the server starts
        try:
            self.server = LiveServer(stack=self.stack)
        except Exception:
            os.environ.pop("ASTRA_ASGI_THREADS", None)
            raise

    def tearDown(self):
        self.server.stop()
        if self._env is None:
            os.environ.pop("ASTRA_ASGI_THREADS", None)
        else:
            os.environ["ASTRA_ASGI_THREADS"] = self._env
        self.stack["store"].close()

    def test_idle_sse_connection_leaves_the_pool_free(self):
        # an idle feed: no after_id, so nothing is replayed and nothing is
        # written until an event arrives
        stream = urllib.request.urlopen(
            f"{self.server.base}/api/events/stream", timeout=10)
        try:
            self.assertEqual(stream.status, 200)
            self.assertIn("text/event-stream",
                          _hdr(dict(stream.headers), "Content-Type", ""))
            time.sleep(0.5)  # give a wrongly-held worker slot time to be held

            started = time.time()
            status, body, _ = _request(self.server.port, "GET", "/api/health")
            elapsed = time.time() - started
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["ok"])
            self.assertLess(elapsed, 5.0,
                            "an idle SSE feed held the only worker thread")
        finally:
            stream.close()


if __name__ == "__main__":
    unittest.main()
