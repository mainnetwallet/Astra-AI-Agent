"""Astra web server: serves the plugin-driven UI and a shared JSON API.

Zero dependencies — stdlib http.server. Threaded so plugins that do slow
work (network research) don't block the UI. Routes come from each plugin's
`routes()`; a small matcher turns `("PATCH", ("api","wallets","<id>"), h)`
into a call with `params={"id": <int>}`.

Core (non-plugin) endpoints:
  GET  /api/manifest    -> {name:"Astra AI Agent", plugins:[tabs]}
  POST /api/chat        -> {message} -> agent reply {reply, action, data, ok}
  GET  /api/dashboard   -> aggregated plugin summary() blocks
  GET  /api/export      -> aggregated plugin export()
  POST /api/import      -> aggregate import across plugins
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .agent import Agent
from .core import Plugin

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")

AGENT_NAME = "Astra AI Agent"

CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "js": "application/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "svg": "image/svg+xml",
    "png": "image/png",
}


def _json(handler, payload, code=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _json_ok(handler, payload, code=200):
    _json(handler, payload, code)


def _json_err(handler, message, code=400):
    _json(handler, {"ok": False, "error": message}, code)


def _route_matcher(route_parts, path_parts):
    """Match a route pattern like ("api","wallets","<id>") against real path
    segments. Returns a params dict (id -> int) or None."""
    if len(route_parts) != len(path_parts):
        return None
    params = {}
    for pat, real in zip(route_parts, path_parts):
        if pat == "<id>":
            if not real.isdigit():
                return None
            params["id"] = int(real)
        elif pat != real:
            return None
    return params


class AstraHandler(BaseHTTPRequestHandler):
    server: "AstraServer"

    def log_message(self, fmt, *args):  # silence request noise
        pass

    # -- plumbing ------------------------------------------------------------
    def _path_parts(self) -> list[str]:
        return [p for p in urlparse(self.path).path.split("/") if p]

    def _query(self) -> dict:
        q = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in q.items()}

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _send_static(self, rel: str) -> None:
        if rel in ("", "/"):
            rel = "index.html"
        rel = rel.lstrip("/")
        if ".." in rel:
            _json_err(self, "bad path", 400)
            return
        root = os.path.abspath(STATIC_DIR)
        path = os.path.abspath(os.path.join(root, rel))
        if not path.startswith(root) or not os.path.isfile(path):
            self.send_error(404, "not found")
            return
        ctype = CONTENT_TYPES.get(path.rsplit(".", 1)[-1], "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- dispatch ------------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        server, path = self.server, self._path_parts()
        q, body = self._query(), self._read_json()
        try:
            # static / manifest first
            if not path:
                return self._send_static("index.html")
            if path[0] == "static":
                return self._send_static("/".join(path[1:]))
            if path[0] == "favicon.ico":
                self.send_response(204)
                return self.end_headers()

            if path == ["api", "manifest"]:
                return _json_ok(self, {"ok": True, "data": server.manifest()})
            if path == ["api", "chat"] and method == "POST":
                reply = server.agent.handle(body.get("message", ""))
                return _json_ok(self, {"ok": True, "data": reply})
            if path == ["api", "dashboard"] and method == "GET":
                return _json_ok(self, {"ok": True, "data": server.agent.dashboard()})
            if path == ["api", "export"] and method == "GET":
                return _json_ok(self, {"ok": True, "data": server.agent.export_all()})
            if path == ["api", "import"] and method == "POST":
                result = server.agent.import_all(body.get("data", body))
                return _json_ok(self, {"ok": True, "data": result})

            # plugin routes
            for p in server.plugins:
                for r_method, r_parts, handler in p.routes():
                    if r_method != method:
                        continue
                    params = _route_matcher(r_parts, path)
                    if params is None:
                        continue
                    status, payload = handler(server.store, params, body, q)
                    return _json_ok(self, payload, status)
            return _json_err(self, "unknown route", 404)
        except Exception as e:
            return _json_err(self, f"internal error: {e}", 500)

    def do_GET(self):    return self._dispatch("GET")
    def do_POST(self):   return self._dispatch("POST")
    def do_PATCH(self):  return self._dispatch("PATCH")
    def do_DELETE(self): return self._dispatch("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


class AstraServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, store, agent: Agent, plugins: list[Plugin]):
        super().__init__(addr, AstraHandler)
        self.store = store
        self.agent = agent
        self.plugins = plugins

    def manifest(self) -> dict:
        """Frontend bootstrap: agent name + one tab descriptor per plugin plus
        the always-on Assistant/Backup/core tabs."""
        tabs = [{"tab": "dashboard", "label": "📊 Dashboard", "core": True}]
        for p in self.plugins:
            tabs.append({"tab": p.slug, "label": f"{p.icon} {p.title}",
                         "plugin": p.slug, "js": f"/static/js/plugins/{p.slug}.js"})
        tabs.append({"tab": "assistant", "label": "🤖 Assistant", "core": True})
        tabs.append({"tab": "backup", "label": "💾 Backup", "core": True})
        return {
            "name": AGENT_NAME,
            "version": getattr(__import__("astra"), "__version__", "1.0.0"),
            "plugins": [{"slug": p.slug, "title": p.title, "icon": p.icon,
                         "order": p.order, "version": p.version,
                         "description": p.description} for p in self.plugins],
            "tabs": tabs,
        }