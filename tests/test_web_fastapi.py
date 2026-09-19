"""Parity tests: the stdlib server and the FastAPI server must answer
identically.

Both are adapters over `astra.web_core`, so any divergence here is a bug in
an adapter (a dropped security header, a different status, a rewritten
body) rather than a difference in behaviour anyone asked for. The ASGI half
is skipped when the optional fastapi/uvicorn extra is not installed — the
core half always runs.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from astra.store import Store
from astra.web import AstraServer
from astra.web_core import AstraSite, Request, WebApp

try:
    import fastapi  # noqa: F401
    import uvicorn
    HAVE_FASTAPI = True
except Exception:  # pragma: no cover - depends on the extra
    HAVE_FASTAPI = False


def _stack():
    from tests.helpers import make_stack
    return make_stack()


# ── core-only tests (no framework needed) ───────────────────────────────────
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

    def test_path_traversal_blocked(self):
        resp = self._call("GET", "/static/../run.py")
        self.assertEqual(resp.status, 400)

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
        self.site.max_body_bytes = 1024
        site = self.site
        from astra.web_core import check_body_size
        self.assertIsNotNone(check_body_size(site, 4096))
        self.assertIsNone(check_body_size(site, 10))

    def test_stream_response_has_no_content_length(self):
        from astra.web_core import response_headers
        resp = self.app.handle(Request("GET", "/api/events/stream"))
        self.assertEqual(resp.status, 200)
        self.assertIsNotNone(resp.stream)
        names = [n.lower() for n, _ in response_headers(resp, Request("GET", "/"), self.site)]
        self.assertNotIn("content-length", names)


class CompatHelperTests(unittest.TestCase):
    """`astra.web._json*` stayed importable for any existing caller; they must
    produce the same hardened envelope as the router does."""

    class _FakeHandler:
        command = "GET"

        def __init__(self):
            self._rid = "rid1"
            self.emitted = None

        def _last_request(self):
            return Request("GET", "/api/health", rid=self._rid)

        def _emit(self, resp, req, method):
            self.emitted = resp

    def test_json_err_envelope(self):
        from astra.web import _json_err
        h = self._FakeHandler()
        _json_err(h, "boom", 400, "bad_request")
        body = json.loads(h.emitted.body.decode("utf-8"))
        self.assertEqual(h.emitted.status, 400)
        self.assertEqual(body["error"], "boom")
        self.assertEqual(body["error_code"], "bad_request")
        self.assertEqual(body["request_id"], "rid1")
        self.assertFalse(body["ok"])

    def test_json_ok_adds_request_id(self):
        from astra.web import _json_ok
        h = self._FakeHandler()
        _json_ok(h, {"ok": True, "data": {}})
        body = json.loads(h.emitted.body.decode("utf-8"))
        self.assertEqual(h.emitted.status, 200)
        self.assertEqual(body["request_id"], "rid1")


# ── live parity between the two servers ─────────────────────────────────────
class _UvicornThread:
    """uvicorn on an ephemeral port, handed a pre-bound socket so there is
    no bind race with the stdlib server."""

    def __init__(self, app):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/api/health", timeout=1).read()
                return
            except Exception:
                time.sleep(0.1)
        raise AssertionError("uvicorn did not become ready")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)


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
        return e.code, e.read(), dict(e.headers)


def _hdr(headers, name, default=None):
    """Case-insensitive header lookup.

    Header names are case-insensitive per RFC 9110, and the two stacks emit
    different casing (stdlib sends the name as written, uvicorn lower-cases
    everything), so compare them the way a client must.
    """
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return default


def _strip_volatile(value):
    """Drop fields that legitimately differ between two live servers."""
    if isinstance(value, dict):
        return {k: _strip_volatile(v) for k, v in value.items()
                if k not in ("request_id", "uptime_s", "started")}
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


@unittest.skipUnless(HAVE_FASTAPI, "fastapi/uvicorn extra not installed")
class ServerParityTests(unittest.TestCase):
    """Same stack, two servers, byte-comparable answers."""

    @classmethod
    def setUpClass(cls):
        from astra.web_fastapi import make_app
        cls.stack = _stack()
        cls.stdlib = AstraServer(("127.0.0.1", 0), cls.stack["store"],
                                 cls.stack["agent"], stack=cls.stack)
        cls.stdlib_port = cls.stdlib.server_address[1]
        cls.thread = threading.Thread(target=cls.stdlib.serve_forever,
                                      daemon=True)
        cls.thread.start()
        # One shared site, so both servers run against the same store and the
        # same config knobs (the ASGI side gets the stack, never a second one).
        cls.asgi_site = AstraSite(("127.0.0.1", 0), cls.stack["store"],
                                  cls.stack["agent"], stack=cls.stack)
        cls.asgi = _UvicornThread(make_app(site=cls.asgi_site))
        cls.asgi.start()

    @classmethod
    def tearDownClass(cls):
        cls.asgi.stop()
        cls.stdlib.shutdown()
        cls.stdlib.server_close()
        cls.stack["store"].close()

    def _both(self, method, path, body=None, headers=None):
        a = _request(self.stdlib_port, method, path, body, headers)
        b = _request(self.asgi.port, method, path, body, headers)
        return a, b

    def _assert_parity(self, path, method="GET", body=None, headers=None,
                       compare_body=True, hardened=True):
        (sa, ba, ha), (sb, bb, hb) = self._both(method, path, body, headers)
        self.assertEqual(sa, sb, f"status differs for {method} {path}")
        # security headers are the contract that must never drift
        if hardened:
            for name in ("X-Content-Type-Options", "X-Frame-Options",
                         "Content-Security-Policy", "Referrer-Policy",
                         "Cross-Origin-Opener-Policy"):
                self.assertEqual(_hdr(ha, name), _hdr(hb, name),
                                 f"{name} differs for {method} {path}")
                self.assertIsNotNone(_hdr(hb, name),
                                     f"{name} missing from the ASGI response "
                                     f"for {method} {path}")
        self.assertTrue(_hdr(ha, "X-Request-Id") and _hdr(hb, "X-Request-Id"),
                        f"missing request id for {method} {path}")
        if compare_body and "application/json" in _hdr(ha, "Content-Type", ""):
            self.assertEqual(_strip_volatile(json.loads(ba)),
                             _strip_volatile(json.loads(bb)),
                             f"body differs for {method} {path}")
        return sa, ba, ha

    # -- stable JSON surfaces ------------------------------------------------
    def test_manifest(self):
        self._assert_parity("/api/manifest")

    def test_tools(self):
        self._assert_parity("/api/tools")

    def test_models(self):
        self._assert_parity("/api/v1/models")

    def test_router_status(self):
        self._assert_parity("/api/v1/router/status")

    def test_web3_policy(self):
        self._assert_parity("/api/v1/web3/transaction-policy")

    def test_health(self):
        self._assert_parity("/api/health")

    def test_static_index_is_byte_identical(self):
        self._assert_parity("/")

    def test_js_asset_is_byte_identical(self):
        self._assert_parity("/static/js/astra.js")

    # -- error surfaces ------------------------------------------------------
    def test_unknown_route(self):
        self._assert_parity("/api/v1/does-not-exist")

    def test_missing_transaction(self):
        self._assert_parity("/api/v1/web3/transactions/nope")

    def test_mode_change_without_token(self):
        self._assert_parity("/api/v1/web3/transaction-policy/mode",
                            method="POST", body={"mode": "AUTO"})

    def test_traversal(self):
        self._assert_parity("/static/../run.py")

    def test_favicon_is_204(self):
        # the favicon 204 carries only the request id on both servers
        (sa, _, ha), (sb, _, hb) = self._both("GET", "/favicon.ico")
        self.assertEqual(sa, sb)
        self.assertEqual(sa, 204)
        self.assertTrue(_hdr(ha, "X-Request-Id") and _hdr(hb, "X-Request-Id"))
        # RFC 9110: a 204 must not carry Content-Length
        self.assertIsNone(_hdr(ha, "Content-Length"))
        self.assertIsNone(_hdr(hb, "Content-Length"))

    # -- preflight -----------------------------------------------------------
    def test_options_preflight_matches(self):
        (sa, _, ha), (sb, _, hb) = self._both(
            "OPTIONS", "/api/health", headers={"Origin": "http://localhost:8787"})
        self.assertEqual(sa, sb)
        self.assertEqual(sa, 204)
        self.assertEqual(_hdr(ha, "Access-Control-Allow-Origin"),
                         _hdr(hb, "Access-Control-Allow-Origin"))
        self.assertEqual(_hdr(ha, "Access-Control-Allow-Methods"),
                         _hdr(hb, "Access-Control-Allow-Methods"))
        self.assertEqual(_hdr(ha, "Access-Control-Allow-Methods"),
                         "GET,POST,PATCH,DELETE,OPTIONS")
        self.assertIsNone(_hdr(ha, "Content-Length"))
        self.assertIsNone(_hdr(hb, "Content-Length"))

    # -- mutations reach the same store --------------------------------------
    def test_memory_round_trip_both_servers(self):
        _request(self.stdlib_port, "POST", "/api/memory",
                 {"content": "parity memo", "category": "note"})
        a = _request(self.stdlib_port, "GET",
                     "/api/memory/search?query=parity&k=1")[1]
        b = _request(self.asgi.port, "GET",
                     "/api/memory/search?query=parity&k=1")[1]
        self.assertEqual(json.loads(a)["data"][0]["content"],
                         json.loads(b)["data"][0]["content"])

    def test_chat_turn_answers_on_both(self):
        # The real agent turn is slow when no provider is configured (it
        # walks the provider retry/timeout path), and what is being tested
        # here is the *adapter*: body decoding, chat-log pinning, and the
        # JSON envelope. Swap in a stub so the check is fast and exact.
        class _StubAgent:
            def handle(self, msg, context="", attachments=None):
                return {"ok": True, "reply": f"echo: {msg}",
                        "action": None, "data": {}}

        real_stdlib, real_asgi = self.stdlib.agent, self.asgi_site.agent
        self.stdlib.agent = self.asgi_site.agent = _StubAgent()
        try:
            a = _request(self.stdlib_port, "POST", "/api/chat", {"message": "hi"})
            b = _request(self.asgi.port, "POST", "/api/chat", {"message": "hi"})
            self.assertEqual(a[0], b[0])
            self.assertEqual(a[0], 200)
            da, db = json.loads(a[1])["data"], json.loads(b[1])["data"]
            self.assertEqual(da["reply"], "echo: hi")
            self.assertEqual(da["reply"], db["reply"])
            self.assertEqual(da["ok"], db["ok"])
            # the reply is pinned to the chat the turn belongs to
            self.assertIsInstance(da["conversation_id"], int)
        finally:
            self.stdlib.agent, self.asgi_site.agent = real_stdlib, real_asgi

    # -- streaming -----------------------------------------------------------
    def test_sse_streams_frames(self):
        # An event must exist and `after_id=0` must be used, otherwise the
        # feed (correctly) idles without writing anything and the read blocks.
        self.stack["events"].emit("ai.completed", agent="parity")

        def read_frames(port):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/events/stream?after_id=0")
            with urllib.request.urlopen(req, timeout=15) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/event-stream",
                              _hdr(dict(resp.headers), "Content-Type", ""))
                return resp.read(1)

        self.assertEqual(read_frames(self.stdlib_port), read_frames(self.asgi.port))

    def test_auth_token_gate_matches(self):
        self.stack["config"].set("ASTRA_TOKEN", "sekrit")
        self.stdlib.operator_token = "sekrit"
        self.asgi_site.operator_token = "sekrit"
        try:
            a = _request(self.stdlib_port, "GET", "/api/health")
            b = _request(self.asgi.port, "GET", "/api/health")
            self.assertEqual(a[0], 401)
            self.assertEqual(b[0], 401)
            self.assertEqual(json.loads(a[1])["error_code"],
                             json.loads(b[1])["error_code"])
            # static stays public on both
            self.assertEqual(_request(self.stdlib_port, "GET", "/")[0], 200)
            self.assertEqual(_request(self.asgi.port, "GET", "/")[0], 200)
        finally:
            self.stack["config"].set("ASTRA_TOKEN", "")
            self.stdlib.operator_token = ""
            self.asgi_site.operator_token = ""


class _FakeScheduler:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


async def _drive_lifespan(app):
    """Run an ASGI app's lifespan startup+shutdown without uvicorn."""
    async with app.router.lifespan_context(app):
        pass


@unittest.skipUnless(HAVE_FASTAPI, "fastapi/uvicorn extra not installed")
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

    def test_sse_response_offers_an_async_drive(self):
        """Locks in the design: the feed must expose an async generator, so an
        ASGI server never has to drive it inside a worker thread."""
        import inspect
        self.assertTrue(inspect.isasyncgenfunction(
            __import__("astra.web_core", fromlist=["x"]).sse_frames_async))
        stack = _stack()
        site = AstraSite(("127.0.0.1", 0), stack["store"], stack["agent"],
                         stack=stack)
        resp = WebApp(site).handle(Request("GET", "/api/events/stream"))
        self.assertTrue(resp.has_stream)
        self.assertIsNotNone(resp.astream)
        stack["store"].close()


@unittest.skipUnless(HAVE_FASTAPI, "fastapi/uvicorn extra not installed")
class WorkerPoolTests(unittest.TestCase):
    """An idle SSE tab must not consume a worker thread.

    The shared pool is set to a single thread for this test, so if the live
    feed held a slot while idle, the ordinary request below could never be
    served. With the async drive it is served immediately.
    """

    def setUp(self):
        from astra.web_fastapi import make_app
        self.stack = _stack()
        self._env = os.environ.get("ASTRA_ASGI_THREADS")
        os.environ["ASTRA_ASGI_THREADS"] = "1"
        # the limit is applied on ASGI startup, so the env var must be set
        # before the server starts
        self.server = _UvicornThread(make_app(stack=self.stack))
        try:
            self.server.start()
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
            f"http://127.0.0.1:{self.server.port}/api/events/stream", timeout=10)
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
