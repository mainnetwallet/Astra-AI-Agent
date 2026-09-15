"""Astra web server: serves the plugin-driven UI and a shared JSON API.

Zero dependencies — stdlib http.server. Threaded so plugins that do slow
work (network research) don't block the UI. Routes come from each plugin's
`routes()`; a small matcher turns `("PATCH", ("api","wallets","<id>"), h)`
into a call with `params={"id": <int>}`.

Core (non-plugin) endpoints (all old routes stay backward-compatible):

  GET  /api/manifest        agent name + plugin tabs + core tabs
  POST /api/chat            {message} -> agent reply {reply, action, data, ok}
  GET  /api/dashboard       aggregated plugin summary() blocks
  GET  /api/export          aggregated plugin export()
  POST /api/import          aggregate import across plugins

New system endpoints:

  GET  /api/health          database/plugins/providers/scheduler diagnostics
  GET  /api/config          public (non-secret) config snapshot
  GET  /api/events          recent live events; ?after_id= for tailing
  GET  /api/events/stream   Server-Sent Events feed (Live tab)
  GET  /api/tools           universal tool registry listing
  GET/POST /api/tasks       generic task engine (task/{id} GET/…)
  GET/POST /api/memory      memory save/list; /api/memory/search
  GET  /api/experiences     experience memory
  GET/POST /api/workflows   workflow definitions; runs via POST {id}/run
  GET/POST /api/schedules   scheduler CRUD
  GET  /api/providers       AI provider health/latency/cost
  GET  /api/agents          recent executions; GET {exec}/… state
  POST /api/agents          submit a goal to the orchestrator
  POST /api/agents/{exec}/resume | /cancel   control WAITING_USER runs
  GET  /api/executions      alias for /api/agents
  GET  /api/plugins         list plugins (+enabled); POST {slug}/enable|disable
"""
from __future__ import annotations

import json
import os
import time
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

CORE_TABS = [
    {"tab": "dashboard", "label": "📊 Dashboard", "core": True},
    {"tab": "live", "label": "⚡ Live", "core": True},
    {"tab": "assistant", "label": "🤖 Assistant", "core": True},
    {"tab": "backup", "label": "💾 Backup", "core": True},
]


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
        elif pat == "<slug>":
            params["slug"] = real
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
            if not path:
                return self._send_static("index.html")
            if path[0] == "static":
                return self._send_static("/".join(path[1:]))
            if path[0] == "favicon.ico":
                self.send_response(204)
                return self.end_headers()

            # system / manifest endpoints
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
            if path == ["api", "health"] and method == "GET":
                return _json_ok(self, {"ok": True, "data": server.health()})
            if path == ["api", "config"] and method == "GET":
                return _json_ok(self, {"ok": True, "data": server.cfg().all()})

            # live events
            if path == ["api", "events"] and method == "GET":
                return _json_ok(self, {"ok": True, "data": server.events().history(
                    limit=int(q.get("limit", 100)), after_id=int(q.get("after_id") or 0))})
            if path == ["api", "events", "stream"] and method == "GET":
                return self._sse()
            if path == ["api", "events", "last"] and method == "GET":
                return _json_ok(self, {"ok": True,
                                       "data": {"last_id": server.events().last_id()}})

            # tools / tasks / memory / experiences
            if path == ["api", "tools"] and method == "GET":
                return _json_ok(self, {"ok": True,
                                       "data": {"tools": server.registry().list(),
                                                "stats": server.registry().stats()}})
            if path == ["api", "tasks"] and method == "GET":
                return _json_ok(self, {"ok": True,
                                       "data": server.tasks().list(
                                           status=q.get("status") or None,
                                           type=q.get("type") or None)})
            if path == ["api", "tasks"] and method == "POST":
                # airdrop plugin's task schema uses "airdrop_id" — yield to the
                # plugin route so old API users keep working untouched
                if body.get("airdrop_id") is not None:
                    pass  # handled below (plugin route loop)
                else:
                    goal = (body.get("goal") or "").strip()
                    if not goal:
                        return _json_err(self, "goal required")
                    t = server.tasks().create(goal=goal, type=body.get("type", "manual"),
                                              priority=int(body.get("priority", 0)),
                                              description=body.get("description", ""))
                    return _json_ok(self, {"ok": True, "data": t}, 201)
            if len(path) == 3 and path[0] == "api" and path[1] == "tasks" and method == "GET":
                # generic task engine GET only handles list; per-id tasks
                # are owned by plugin routes (e.g. airdrop tasks PATCH)
                t = server.tasks().get(int(path[2])) if path[2].isdigit() else None
                if not t:
                    return _json_err(self, "task not found", 404)
                return _json_ok(self, {"ok": True, "data": t})
            if path == ["api", "memory"] and method == "GET":
                return _json_ok(self, {"ok": True, "data": server.memory().all(
                    limit=int(q.get("limit", 100)), category=q.get("category") or None)})
            if path == ["api", "memory"] and method == "POST":
                content = (body.get("content") or "").strip()
                if not content:
                    return _json_err(self, "content required")
                m = server.memory().save(content, body.get("category", "note"),
                                         body.get("tags", ""), source="api")
                return _json_ok(self, {"ok": True, "data": m}, 201)
            if path == ["api", "memory", "search"] and method == "GET":
                qq = q.get("query", "")
                return _json_ok(self, {"ok": True,
                                       "data": server.memory().search(qq, k=int(q.get("k", 5)))})
            if len(path) == 3 and path[0] == "api" and path[1] == "memory" and method == "DELETE":
                server.memory().forget(path[2])
                return _json_ok(self, {"ok": True})
            if path == ["api", "experiences"] and method == "GET":
                return _json_ok(self, {"ok": True, "data": server.experiences().stats()})

            # workflows + scheduler
            if path == ["api", "workflows"] and method == "GET":
                return _json_ok(self, {"ok": True,
                                       "data": server.workflows().list_definitions()})
            if path == ["api", "workflows"] and method == "POST":
                steps = body.get("steps") or []
                if not isinstance(steps, list):
                    return _json_err(self, "steps must be a list")
                wf = server.workflows().define(body.get("name", ""), body.get("description", ""), steps)
                return _json_ok(self, {"ok": True, "data": wf}, 201)
            if len(path) == 4 and path[:2] == ["api", "workflows"] and path[3] == "run" and method == "POST":
                run = server.workflows().run(workflow_id=int(path[2]), params=body.get("params") or {})
                return _json_ok(self, {"ok": True, "data": run})
            if path == ["api", "workflows", "runs"] and method == "GET":
                return _json_ok(self, {"ok": True, "data": server.workflows().list_runs()})
            if path == ["api", "schedules"] and method == "GET":
                sched = server.scheduler()
                return _json_ok(self, {"ok": True,
                                       "data": (sched.list() if sched else [])})
            if path == ["api", "schedules"] and method == "POST" and server.scheduler():
                s = server.scheduler().add(body.get("name", "sched"), body.get("kind", "daily"),
                                           body.get("value", ""), int(body.get("workflow_id", 0)),
                                           body.get("params") or {})
                return _json_ok(self, {"ok": True, "data": s}, 201)
            if len(path) == 3 and path[:2] == ["api", "schedules"] and method in ("PATCH", "DELETE"):
                sc = server.scheduler()
                if not sc:
                    return _json_err(self, "scheduler disabled", 400)
                if method == "DELETE":
                    sc.delete(int(path[2]))
                    return _json_ok(self, {"ok": True})
                # PATCH: enable/disable only (schedule fields are fixed)
                enabled = body.get("enabled")
                if enabled is None:
                    return _json_err(self, "enabled required")
                row = sc.set_enabled(int(path[2]), bool(enabled))
                return _json_ok(self, {"ok": True, "data": row}) if row else \
                    _json_err(self, "schedule not found", 404)

            # ai providers
            if path == ["api", "providers"] and method == "GET":
                return _json_ok(self, {"ok": True,
                                       "data": server.router().stats()})

            # orchestrator / agents
            if path == ["api", "agents"] and method == "GET":
                return _json_ok(self, {"ok": True,
                                       "data": {"recent": server.orchestrator().recent(),
                                                "stats": server.orchestrator().stats()}})
            if path == ["api", "agents"] and method == "POST":
                goal = (body.get("goal") or "").strip()
                if not goal:
                    return _json_err(self, "goal required")
                r = server.orchestrator().submit(goal, sync=bool(body.get("sync", False)))
                return _json_ok(self, {"ok": True, "data": r}, 201)
            if len(path) == 3 and path[0] == "api" and path[1] == "agents" and method == "GET":
                return _json_ok(self, {"ok": True,
                                       "data": server.orchestrator().state(path[2])})
            if len(path) == 4 and path[0] == "api" and path[1] == "agents" and path[3] == "resume" and method == "POST":
                return _json_ok(self, {"ok": True,
                                       "data": server.orchestrator().resume(path[2], bool(body.get("allow", True)))})
            if len(path) == 4 and path[0] == "api" and path[1] == "agents" and path[3] == "cancel" and method == "POST":
                return _json_ok(self, {"ok": True,
                                       "data": server.orchestrator().cancel(path[2])})
            if path == ["api", "executions"] and method == "GET":
                return _json_ok(self, {"ok": True,
                                       "data": server.orchestrator().recent()})

            # plugin management
            if path == ["api", "plugins"] and method == "GET":
                return _json_ok(self, {"ok": True, "data": [
                    {"slug": p.slug, "title": p.title, "icon": p.icon,
                     "enabled": bool(getattr(p, "enabled", True)),
                     "version": getattr(p, "version", ""),
                     "health": (getattr(p, "health", None) or
                                ({**p.health_check()} if hasattr(p, "health_check") else {}))}
                    for p in server.plugins]})
            if len(path) == 4 and path[0] == "api" and path[1] == "plugins" and path[3] in ("enable", "disable") and method == "POST":
                slug, want = path[2], path[3] == "enable"
                for p in server.plugins:
                    if p.slug == slug:
                        p.enabled = want
                        server.registry_plugin_tools()
                        # persist to store
                        store_inst = server.store
                        try:
                            from astra.core import Registry as _R
                            _R.set_enabled(store_inst, slug, want)
                        except Exception:
                            pass
                        return _json_ok(self, {"ok": True,
                                               "data": {"slug": slug, "enabled": want}})
                return _json_err(self, f"plugin not found: {slug}", 404)

            # plugin routes (only enabled plugins answer)
            for p in server.plugins:
                if not getattr(p, "enabled", True):
                    continue
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
            return _json_err(self, f"internal error: {type(e).__name__}: {e}", 500)

    # -- SSE ------------------------------------------------------------------
    def _sse(self):
        """Server-Sent Events feed. Tails new events (poll inside the request
        thread); writes `data:` frames and flushes, so the browser Live tab
        streams activity with no WebSocket dependency."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        after = int(self._query().get("after_id") or self.server.events().last_id())
        written, idle, deadline = 0, 0, time.time() + 45
        try:
            while time.time() < deadline:
                evs = self.server.events().since(after)
                if evs:
                    for e in evs:
                        frame = (f"id: {e['id']}\ndata: " +
                                 json.dumps({"id": e["id"], "kind": e["kind"],
                                             "agent": e.get("agent", ""),
                                             "data": e.get("data", {}),
                                             "created_at": e.get("created_at", "")},
                                            ensure_ascii=False) + "\n\n")
                        self.wfile.write(frame.encode("utf-8"))
                        self.wfile.flush()
                        after = e["id"]
                        written += 1
                    idle = 0
                    if written >= 200:
                        break
                else:
                    idle += 1
                    time.sleep(0.7)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass

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

    def __init__(self, addr, store, agent: Agent, plugins: list[Plugin], stack=None):
        super().__init__(addr, AstraHandler)
        self.store = store
        self.agent = agent
        self.plugins = plugins
        self._stack = stack or {}

    # accessors (safe when a subsystem is absent)
    def _get(self, key):
        return self._stack.get(key)

    def events(self):
        return self._get("events")

    def tasks(self):
        return self._get("tasks")

    def memory(self):
        return self._get("memory")

    def experiences(self):
        return self._get("experiences")

    def registry(self):
        return self._get("registry")

    def router(self):
        return self._get("router")

    def workflows(self):
        return self._get("workflows")

    def scheduler(self):
        return self._get("scheduler")

    def orchestrator(self):
        return self._get("orchestrator")

    def cfg(self):
        return self._get("config")

    def health(self) -> dict:
        out = {"app": AGENT_NAME, "ok": True, "checks": {}}
        try:
            out["checks"]["database"] = "ok"
            count = self.store.fetchone("SELECT COUNT(*) c FROM sqlite_master")["c"]
            out["checks"]["schema_objects"] = count
        except Exception as e:
            out["ok"] = False
            out["checks"]["database"] = f"error: {e}"
        plugin_checks = []
        for p in self.plugins:
            try:
                hc = p.health_check() if hasattr(p, "health_check") else {"ok": True}
            except Exception as e:
                hc = {"ok": False, "error": str(e)}
            plugin_checks.append({"slug": p.slug, "enabled": bool(getattr(p, "enabled", True)),
                                  **hc})
        out["checks"]["plugins"] = plugin_checks
        if self.router():
            out["checks"]["providers"] = self.router().health()
        if self.scheduler():
            out["checks"]["scheduler"] = self.scheduler().stats()
        # offline provider is the expected default (offline-first), so a
        # missing API key must not flip /api/health red
        provider_unhealthy = any(
            p.get("healthy") is False and name != "offline"
            for name, p in out["checks"].get("providers", {}).items())
        if any(not c.get("ok", True) for c in plugin_checks) or provider_unhealthy:
            out["ok"] = False
        return out

    def registry_plugin_tools(self):
        """Re-register plugin tools in the registry (enable/disable applies
        to routes + manifest; tools are idempotent to re-add)."""
        reg = self.registry()
        if reg is None:
            return
        for p in self.plugins:
            for spec in (p.tools() or []):
                try:
                    reg.unregister(spec.get("name", ""))
                except Exception:
                    pass
        reg.register_plugin_tools([p for p in self.plugins if getattr(p, "enabled", True)])

    def manifest(self) -> dict:
        """Frontend bootstrap: agent name + one tab descriptor per enabled
        plugin plus the always-on core tabs."""
        tabs = [dict(t) for t in CORE_TABS]
        for p in self.plugins:
            if not getattr(p, "enabled", True):
                continue
            tabs.append({"tab": p.slug, "label": f"{p.icon} {p.title}",
                         "plugin": p.slug, "js": f"/static/js/plugins/{p.slug}.js"})
        return {
            "name": AGENT_NAME,
            "version": getattr(__import__("astra"), "__version__", "1.0.0"),
            "plugins": [{"slug": p.slug, "title": p.title, "icon": p.icon,
                         "order": p.order, "version": p.version,
                         "description": p.description,
                         "enabled": bool(getattr(p, "enabled", True))}
                        for p in self.plugins],
            "tabs": tabs,
        }