"""Shared harness for Astra tests. Import as `from helpers import ...`
(discover -s tests puts this directory on sys.path).
The plugin system has been removed — `plugins/` is an empty placeholder
(see plugins/README.md), so the legacy `plugins` list is always empty."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astra.store import Store              # noqa: E402
from astra.agent import Agent              # noqa: E402


def make_agent():
    """Returns (store, plugins, agent) wired exactly like the real app."""
    store = Store(":memory:")
    plugins = []
    agent = Agent()
    return store, plugins, agent


def make_plugin():
    """No plugins are registered yet (see plugins/README.md)."""
    return None


def make_stack(**kw):
    """Full stack (tools, memory, workflows, scheduler, router, …) on a fresh
    in-memory store — the same wiring run.py uses."""
    from astra.bootstrap import build
    return build(store=Store(":memory:"), **kw)


class LiveServer:
    """The real web server, in this process, on an ephemeral port.

    uvicorn runs in a background thread over a pre-bound socket, so two tests
    can never race for the same port and no sleep-and-hope is needed to know
    it is up.

    `site` is the AstraSite the running app serves from, so a test can flip the
    security knobs exactly where it used to poke the old server object:

        srv = LiveServer(stack=stack)
        srv.site.operator_token = "sekrit"
        srv.site.env = "production"
        srv.site.max_body_bytes = 10 * 1024 * 1024
    """

    def __init__(self, stack=None, store=None, agent=None):
        import socket
        import threading

        import uvicorn

        from astra.web import AstraSite
        from astra.web_fastapi import make_app

        if stack is not None:
            store = store or stack["store"]
            agent = agent or stack["agent"]
        self.stack = stack
        self.site = AstraSite(("127.0.0.1", 0), store, agent, stack=stack)
        # site= -> the caller owns the stack, so nothing is closed for us
        self.app = make_app(site=self.site)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.setblocking(False)
        self.port = self._sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(uvicorn.Config(self.app, log_level="error"))
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._sock]},
            daemon=True)
        self._thread.start()
        self._wait_ready()

    def _wait_ready(self, timeout=20.0):
        import time
        import urllib.error
        import urllib.request
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.base + "/api/health", timeout=1).read()
                return self
            except urllib.error.HTTPError:
                return self  # any HTTP status at all means it is serving
            except Exception:
                time.sleep(0.05)
        raise AssertionError("the ASGI server did not become ready")

    def stop(self):
        self._server.should_exit = True
        self._thread.join(10)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
