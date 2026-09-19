"""Astra web server — FastAPI edition.

Drop-in replacement for `astra/web.py` (stdlib http.server). Every route,
security behaviour and response shape is preserved 1:1; only the HTTP layer
changed. All business logic (Agent, Store, ChatLog, security helpers) is
reused unmodified from the rest of the package.

Preserved behaviour:
  * optional operator auth (ASTRA_TOKEN) — Bearer / X-Astra-Token / ?token=
  * per-IP rate limiting (ASTRA_API_RATE_LIMIT, default 300 req/min)
  * request body cap (ASTRA_MAX_BODY_MB, default 50 MB)
  * security headers + CSP on every response
  * X-Request-Id on every response; request_id in every JSON body
  * structured JSON errors {ok, error, error_code, request_id}
  * every response redacted through security.redact
  * /api/v1/... routes alias /api/... with no duplicated code
  * GET /api/events/stream — Server-Sent Events (now via StreamingResponse)

Run with:  uvicorn astra.web_fastapi:app --host 127.0.0.1 --port 8787
or:        python3 run_fastapi.py
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from typing import Optional

from fastapi import FastAPI, Request, Response, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse

from .agent import Agent
from .chat_log import ChatLog
from .security import ApiError, RateLimiter, make_request_id, redact

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
AGENT_NAME = "Astra AI Agent"

CONTENT_TYPES = {
    "html": "text/html; charset=utf-8", "js": "application/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8", "json": "application/json; charset=utf-8",
    "svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
    "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
    "m4a": "audio/mp4", "aac": "audio/aac", "flac": "audio/flac", "opus": "audio/opus",
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    "pdf": "application/pdf", "csv": "text/csv", "tsv": "text/tab-separated-values",
    "txt": "text/plain; charset=utf-8", "md": "text/plain; charset=utf-8",
    "xml": "application/xml", "yaml": "application/x-yaml", "zip": "application/zip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

CORE_TABS = [
    {"tab": "dashboard", "label": "📊 Dashboard", "core": True},
    {"tab": "assistant", "label": "🤖 Assistant", "core": True},
    {"tab": "providers", "label": "🔌 AI Providers health", "core": True},
    {"tab": "router", "label": "🧠 Router", "core": True},
    {"tab": "web3", "label": "⛓️ Wallet", "core": True},
    {"tab": "backup", "label": "💾 Backup", "core": True},
]
LOGS_TAB = {"tab": "logs", "label": "📡 Activity Log", "core": True}

SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("Referrer-Policy", "no-referrer"),
    ("X-XSS-Protection", "0"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Content-Security-Policy",
     "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
     "style-src 'self' 'unsafe-inline'; script-src 'self'; "
     "connect-src 'self'; frame-ancestors 'self'"),
]


# ---------------------------------------------------------------------------
# AstraState — same role as the old AstraServer: holds shared subsystems and
# security knobs, built once at startup from the same `build()` stack.
# ---------------------------------------------------------------------------
class AstraState:
    def __init__(self, store, agent: Agent, stack=None):
        self.store = store
        self.agent = agent
        self.chat_log = ChatLog(store, redact=redact)
        self._stack = stack or {}
        cfg = self._get("config")
        self.env = (cfg.get("ENV") if cfg else None) or os.environ.get("ENV", "development")
        self.operator_token = (cfg.get("ASTRA_TOKEN") if cfg else None) \
            or os.environ.get("ASTRA_TOKEN") or os.environ.get("ASTRA_API_KEY", "")
        self.max_body_bytes = int((cfg.getint("ASTRA_MAX_BODY_MB", 50) if cfg else 50) * 1024 * 1024)
        try:
            rl = (cfg.getint("ASTRA_API_RATE_LIMIT", 300) if cfg else 300)
        except Exception:
            rl = 300
        self.rate_limiter = RateLimiter(rl, 60.0)
        self._allowed_origins = set((cfg.getlist("ASTRA_CORS_ORIGINS") if cfg else None) or [])
        self._req_lock = asyncio.Lock()
        self._req = {"count": 0, "errors": 0, "by_path": {},
                     "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._metrics_start = time.time()

    def _get(self, key):
        return self._stack.get(key)

    def events(self): return self._get("events")
    def tasks(self): return self._get("tasks")
    def memory(self): return self._get("memory")
    def experiences(self): return self._get("experiences")
    def registry(self): return self._get("registry")
    def router(self): return self._get("router")
    def workflows(self): return self._get("workflows")
    def scheduler(self): return self._get("scheduler")
    def orchestrator(self): return self._get("orchestrator")
    def cfg(self): return self._get("config")

    def cors_origin(self, origin):
        if not origin:
            return None
        if self._allowed_origins:
            if "*" in self._allowed_origins:
                return "*"
            return origin if origin in self._allowed_origins else None
        if self.env == "production":
            return None
        return origin

    async def note_request(self, method, path, error=False):
        async with self._req_lock:
            self._req["count"] += 1
            if error:
                self._req["errors"] += 1
            label = "/".join(path) if path else "/"
            blob = self._req["by_path"].setdefault(label, {"count": 0, "errors": 0})
            blob["count"] += 1
            if error:
                blob["errors"] += 1

    def metrics(self) -> dict:
        out = {"app": AGENT_NAME, "uptime_s": round(time.time() - self._metrics_start, 1),
               "requests": dict(self._req)}
        if self.router():
            out["router"] = {name: {k: info[k] for k in
                                    ("healthy", "state", "calls", "errors", "latency_avg_ms")
                                    if k in info}
                             for name, info in self.router().health().items()}
        reg = self.registry()
        if reg:
            all_stats = reg.stats()
            out["tools"] = {"count": len(all_stats),
                            "calls": sum(s["calls"] for s in all_stats.values()),
                            "errors": sum(s["errors"] for s in all_stats.values())}
        orch = self.orchestrator()
        if orch:
            out["orchestrator"] = {k: v for k, v in orch.stats().items() if not isinstance(v, dict)}
        tx = self._get("tx_manager")
        if tx:
            txs = tx.stats()
            pol = self._get("web3_policy") or self._get("tx_policy")
            out["web3"] = {"status_counts": txs.get("by_status", {}),
                           "mode": (pol.mode if pol else None),
                           "stopped": bool(getattr(tx, "stopped", False))}
        if self.scheduler():
            out["scheduler"] = self.scheduler().stats()
        return out

    def health(self) -> dict:
        out = {"app": AGENT_NAME, "ok": True, "checks": {}}
        try:
            out["checks"]["database"] = "ok"
            count = self.store.fetchone("SELECT COUNT(*) c FROM sqlite_master")["c"]
            out["checks"]["schema_objects"] = count
        except Exception as e:
            out["ok"] = False
            out["checks"]["database"] = f"error: {e}"
        out["checks"]["plugins"] = []
        if self.router():
            out["checks"]["providers"] = self.router().health()
        if self.scheduler():
            out["checks"]["scheduler"] = self.scheduler().stats()
        provider_unhealthy = any(
            p.get("healthy") is False
            for name, p in out["checks"].get("providers", {}).items())
        if provider_unhealthy:
            out["ok"] = False
        return out

    def manifest(self) -> dict:
        tabs = [dict(t) for t in CORE_TABS]
        tabs.append(dict(LOGS_TAB))
        return {"name": AGENT_NAME,
                "version": getattr(__import__("astra"), "__version__", "1.0.0"),
                "plugins": [], "tabs": tabs}


def make_app(store, agent: Agent, stack=None) -> FastAPI:
    """Build the FastAPI app. Call this from your launcher instead of
    importing a bare module-level `app`, since state depends on the built
    stack (store/agent/config/etc.)."""
    state = AstraState(store, agent, stack)
    app = FastAPI(title=AGENT_NAME, docs_url="/api/docs", redoc_url="/api/redoc",
                  openapi_url="/api/openapi.json")
    app.state.astra = state

    # -- helpers --------------------------------------------------------
    def _rid(request: Request) -> str:
        return getattr(request.state, "rid", "")

    def json_ok(request: Request, payload, code: int = 200) -> JSONResponse:
        rid = _rid(request)
        if isinstance(payload, dict) and "request_id" not in payload:
            payload = {**payload, "request_id": rid}
        resp = JSONResponse(content=redact(payload), status_code=code)
        resp.headers["X-Request-Id"] = rid
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def json_err(request: Request, message: str, code: int = 400,
                 error_code: str = "bad_request") -> JSONResponse:
        return json_ok(request, {"ok": False, "error": message, "error_code": error_code}, code)

    def authorized(request: Request) -> bool:
        token = state.operator_token
        if not token:
            return True
        supplied = request.headers.get("Authorization", "")
        if supplied.lower().startswith("bearer "):
            candidate = supplied[7:].strip()
        else:
            candidate = request.headers.get("X-Astra-Token", "") or request.query_params.get("token", "")
        return bool(candidate) and hmac.compare_digest(candidate, token)

    def rate_limited(request: Request) -> bool:
        limiter = state.rate_limiter
        if not limiter:
            return False
        ip = request.client.host if request.client else "unknown"
        return not limiter.allow(ip)

    # -- global middleware: request id, auth gate, rate limit, headers ---
    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        rid = make_request_id()
        request.state.rid = rid
        path_parts = [p for p in request.url.path.split("/") if p]

        if path_parts and path_parts[0] == "api":
            if not authorized(request):
                resp = json_err(request, "authentication required", 401, "authentication")
                await state.note_request(request.method, path_parts, error=True)
                _apply_headers(resp, request)
                return resp
            if rate_limited(request):
                resp = json_err(request, "rate limit exceeded", 429, "rate_limit")
                await state.note_request(request.method, path_parts, error=True)
                _apply_headers(resp, request)
                return resp

        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > state.max_body_bytes:
            resp = json_err(request, f"request body exceeds {state.max_body_bytes} bytes limit",
                            413, "payload_too_large")
            _apply_headers(resp, request)
            return resp

        try:
            response = await call_next(request)
        except ApiError as e:
            if not e.request_id:
                e.request_id = rid
            await state.note_request(request.method, path_parts, error=True)
            response = JSONResponse(content=redact(e.to_dict()), status_code=e.status)
        except Exception as e:
            await state.note_request(request.method, path_parts, error=True)
            detail = str(e) if state.env != "production" else ""
            msg = f"internal error{': ' + detail if detail else ''}"
            response = JSONResponse(
                content=redact({"ok": False, "error": msg, "error_code": "internal", "request_id": rid}),
                status_code=500)

        await state.note_request(request.method, path_parts)
        response.headers["X-Request-Id"] = rid
        _apply_headers(response, request)
        return response

    def _apply_headers(response: Response, request: Request) -> None:
        for name, value in SECURITY_HEADERS:
            response.headers[name] = value
        origin = request.headers.get("origin")
        allowed = state.cors_origin(origin)
        if allowed:
            response.headers["Access-Control-Allow-Origin"] = allowed
            response.headers["Vary"] = "Origin"

    @app.options("/{full_path:path}")
    async def options_handler(full_path: str, request: Request):
        resp = Response(status_code=204)
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,DELETE,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Astra-Token, Authorization"
        resp.headers["Access-Control-Max-Age"] = "600"
        return resp

    def _v1(path: list[str]) -> list[str]:
        if len(path) >= 3 and path[0] == "api" and path[1] == "v1":
            return ["api"] + path[2:]
        return path

    def _serve_static(rel: str) -> FileResponse:
        if rel in ("", "/"):
            rel = "index.html"
        rel = rel.lstrip("/")
        if ".." in rel or "\\" in rel:
            raise ApiError("bad_path", "bad path", 400)
        root = os.path.abspath(STATIC_DIR)
        path = os.path.abspath(os.path.join(root, rel))
        if not (path == root or path.startswith(root + os.sep)):
            raise ApiError("forbidden", "forbidden", 403)
        if not os.path.isfile(path):
            raise ApiError("not_found", "not found", 404)
        ctype = CONTENT_TYPES.get(path.rsplit(".", 1)[-1], "application/octet-stream")
        headers = {}
        if path.endswith((".html", ".js", ".css")):
            headers["Cache-Control"] = "no-cache, must-revalidate"
        return FileResponse(path, media_type=ctype, headers=headers)

    # -- static / root ----------------------------------------------------
    @app.get("/")
    async def root():
        return _serve_static("index.html")

    @app.get("/static/{rel_path:path}")
    async def static_files(rel_path: str):
        return _serve_static(rel_path)

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    # -- manifest / chat ----------------------------------------------------
    @app.get("/api/manifest")
    @app.get("/api/v1/manifest")
    async def manifest(request: Request):
        return json_ok(request, {"ok": True, "data": state.manifest()})

    async def _process_uploads(files: list) -> list[dict]:
        if not files:
            return []
        upload_dir = os.path.join(os.path.dirname(STATIC_DIR), "data", "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        attachments = []
        from astra.core.attachments import process_upload
        for f in files[:10]:
            try:
                data = await f.read()
                att = process_upload(data, f.filename, upload_dir)
                attachments.append(att.to_dict())
            except Exception as e:
                attachments.append({"filename": getattr(f, "filename", "unknown"),
                                    "error": str(e), "processed": False})
        return attachments

    @app.post("/api/chat")
    @app.post("/api/v1/chat")
    async def chat(request: Request):
        log = state.chat_log
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            form = await request.form()
            msg = form.get("message", "") or ""
            ctx = form.get("context", "") or ""
            files = [v for v in form.values() if isinstance(v, UploadFile)]
            names = [f.filename for f in files[:10]]
            attachments = await _process_uploads(files)
            cid = log.add_user(msg, files=names)
        else:
            body = await request.json() if await request.body() else {}
            msg = body.get("message", "")
            ctx = body.get("context", "") or ""
            attachments = None
            cid = log.add_user(msg)
        token = log.begin(cid)
        try:
            reply = state.agent.handle(msg, context=ctx, attachments=attachments or None)
            log.add_reply(reply, conversation_id=cid)
        finally:
            log.end(token)
        reply = dict(reply)
        reply["conversation_id"] = cid
        return json_ok(request, {"ok": True, "data": reply})

    @app.get("/api/chat/history")
    @app.get("/api/v1/chat/history")
    async def chat_history(request: Request, after_id: int = 0, limit: int = 200,
                            conversation_id: Optional[int] = None):
        return json_ok(request, {"ok": True, "data": state.chat_log.history(
            after_id=after_id, limit=min(limit, 500), conversation_id=conversation_id)})

    @app.delete("/api/chat/history")
    @app.delete("/api/v1/chat/history")
    async def chat_history_clear(request: Request):
        return json_ok(request, {"ok": True, "removed": state.chat_log.clear()})

    @app.get("/api/chat/conversations")
    @app.get("/api/v1/chat/conversations")
    async def chat_conversations(request: Request):
        return json_ok(request, {"ok": True, "data": state.chat_log.list_conversations()})

    @app.post("/api/chat/conversations")
    @app.post("/api/v1/chat/conversations")
    async def chat_conversations_new(request: Request):
        cid = state.chat_log.new_conversation()
        return json_ok(request, {"ok": True, "data": {"id": cid}})

    @app.get("/api/chat/conversations/{cid}")
    @app.get("/api/v1/chat/conversations/{cid}")
    async def chat_conversation_get(request: Request, cid: int):
        if not state.chat_log.switch(cid):
            return json_err(request, "chat not found", 404)
        return json_ok(request, {"ok": True, "data": state.chat_log.history(conversation_id=cid)})

    @app.delete("/api/chat/conversations/{cid}")
    @app.delete("/api/v1/chat/conversations/{cid}")
    async def chat_conversation_delete(request: Request, cid: int):
        if not state.chat_log.delete_conversation(cid):
            return json_err(request, "chat not found", 404)
        return json_ok(request, {"ok": True, "data": {"current": state.chat_log.current_id}})

    @app.post("/api/chat/resume")
    @app.post("/api/v1/chat/resume")
    async def chat_resume(request: Request):
        body = await request.json() if await request.body() else {}
        eid = (body.get("execution_id") or "").strip()
        if not eid:
            return json_err(request, "execution_id required")
        cid = state.chat_log.current_id
        token = state.chat_log.begin(cid)
        try:
            reply = state.agent.resume(eid, bool(body.get("allow", True)))
            state.chat_log.add_reply(reply, conversation_id=cid)
        finally:
            state.chat_log.end(token)
        reply = dict(reply)
        reply["conversation_id"] = cid
        return json_ok(request, {"ok": True, "data": reply})

    # -- dashboard / export / import / health / config -----------------------
    @app.get("/api/dashboard")
    @app.get("/api/v1/dashboard")
    async def dashboard(request: Request):
        return json_ok(request, {"ok": True, "data": state.agent.dashboard()})

    @app.get("/api/export")
    @app.get("/api/v1/export")
    async def export_all(request: Request):
        return json_ok(request, {"ok": True, "data": state.agent.export_all()})

    @app.post("/api/import")
    @app.post("/api/v1/import")
    async def import_all(request: Request):
        body = await request.json() if await request.body() else {}
        result = state.agent.import_all(body.get("data", body))
        return json_ok(request, {"ok": True, "data": result})

    @app.get("/api/health")
    @app.get("/api/v1/health")
    async def health(request: Request):
        return json_ok(request, {"ok": True, "data": state.health()})

    @app.get("/api/config")
    @app.get("/api/v1/config")
    async def config(request: Request):
        return json_ok(request, {"ok": True, "data": state.cfg().all()})

    # -- events / SSE ---------------------------------------------------
    @app.get("/api/events")
    @app.get("/api/v1/events")
    async def events_history(request: Request, limit: int = 100, after_id: int = 0):
        return json_ok(request, {"ok": True, "data": state.events().history(limit=limit, after_id=after_id)})

    @app.delete("/api/events")
    @app.delete("/api/v1/events")
    async def events_clear(request: Request):
        removed = state.events().clear()
        return json_ok(request, {"ok": True, "removed": removed})

    @app.get("/api/events/last")
    @app.get("/api/v1/events/last")
    async def events_last(request: Request):
        return json_ok(request, {"ok": True, "data": {"last_id": state.events().last_id()}})

    @app.get("/api/events/stream")
    @app.get("/api/v1/events/stream")
    async def events_stream(request: Request):
        rid = _rid(request)
        after_header = request.headers.get("Last-Event-ID")
        after_q = request.query_params.get("after_id")
        if after_header:
            after = int(after_header)
        elif after_q:
            after = int(after_q)
        else:
            after = state.events().last_id()

        async def gen():
            written, deadline = 0, time.time() + 45
            cur = after
            while time.time() < deadline:
                if await request.is_disconnected():
                    break
                evs = state.events().since(cur)
                if evs:
                    for e in evs:
                        frame = (f"id: {e['id']}\ndata: " +
                                 json.dumps(redact({"id": e["id"], "kind": e["kind"],
                                             "agent": e.get("agent", ""), "data": e.get("data", {}),
                                             "created_at": e.get("created_at", "")}),
                                            ensure_ascii=False) + "\n\n")
                        yield frame.encode("utf-8")
                        cur = e["id"]
                        written += 1
                    if written >= 200:
                        break
                else:
                    await asyncio.sleep(0.7)

        headers = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Request-Id": rid}
        return StreamingResponse(gen(), media_type="text/event-stream; charset=utf-8", headers=headers)

    # -- tools / tasks / memory / experiences ---------------------------
    @app.get("/api/tools")
    @app.get("/api/v1/tools")
    async def tools(request: Request):
        return json_ok(request, {"ok": True, "data": {"tools": state.registry().list(),
                                                       "stats": state.registry().stats()}})

    @app.get("/api/tasks")
    @app.get("/api/v1/tasks")
    async def tasks_list(request: Request, status: Optional[str] = None, type: Optional[str] = None):
        return json_ok(request, {"ok": True, "data": state.tasks().list(status=status, type=type)})

    @app.post("/api/tasks")
    @app.post("/api/v1/tasks")
    async def tasks_create(request: Request):
        body = await request.json() if await request.body() else {}
        goal = (body.get("goal") or "").strip()
        if not goal:
            return json_err(request, "goal required")
        t = state.tasks().create(goal=goal, type=body.get("type", "manual"),
                                 priority=int(body.get("priority", 0)),
                                 description=body.get("description", ""))
        return json_ok(request, {"ok": True, "data": t}, 201)

    @app.get("/api/tasks/{task_id}")
    @app.get("/api/v1/tasks/{task_id}")
    async def tasks_get(request: Request, task_id: str):
        t = state.tasks().get(int(task_id)) if task_id.isdigit() else None
        if not t:
            return json_err(request, "task not found", 404)
        return json_ok(request, {"ok": True, "data": t})

    @app.get("/api/memory")
    @app.get("/api/v1/memory")
    async def memory_list(request: Request, limit: int = 100, category: Optional[str] = None):
        return json_ok(request, {"ok": True, "data": state.memory().all(limit=limit, category=category)})

    @app.post("/api/memory")
    @app.post("/api/v1/memory")
    async def memory_save(request: Request):
        body = await request.json() if await request.body() else {}
        content = (body.get("content") or "").strip()
        if not content:
            return json_err(request, "content required")
        try:
            imp = float(body.get("importance", 0.5))
        except (TypeError, ValueError):
            imp = 0.5
        m = state.memory().save(content, body.get("category", "note"), body.get("tags", ""),
                                source="api", layer=body.get("layer", "long"), importance=imp)
        return json_ok(request, {"ok": True, "data": m}, 201)

    @app.get("/api/memory/search")
    @app.get("/api/v1/memory/search")
    async def memory_search(request: Request, query: str = "", k: int = 5,
                             layer: Optional[str] = None, min_importance: Optional[float] = None):
        return json_ok(request, {"ok": True, "data": state.memory().search(
            query, k=k, layer=layer, min_importance=min_importance)})

    @app.delete("/api/memory/{mem_id}")
    @app.delete("/api/v1/memory/{mem_id}")
    async def memory_delete(request: Request, mem_id: str):
        state.memory().forget(mem_id)
        return json_ok(request, {"ok": True})

    @app.get("/api/experiences")
    @app.get("/api/v1/experiences")
    async def experiences(request: Request):
        return json_ok(request, {"ok": True, "data": state.experiences().stats()})

    # -- workflows + scheduler -------------------------------------------
    @app.get("/api/workflows")
    @app.get("/api/v1/workflows")
    async def workflows_list(request: Request):
        return json_ok(request, {"ok": True, "data": state.workflows().list_definitions()})

    @app.post("/api/workflows")
    @app.post("/api/v1/workflows")
    async def workflows_define(request: Request):
        body = await request.json() if await request.body() else {}
        steps = body.get("steps") or []
        if not isinstance(steps, list):
            return json_err(request, "steps must be a list")
        wf = state.workflows().define(body.get("name", ""), body.get("description", ""), steps)
        return json_ok(request, {"ok": True, "data": wf}, 201)

    @app.post("/api/workflows/{wf_id}/run")
    @app.post("/api/v1/workflows/{wf_id}/run")
    async def workflows_run(request: Request, wf_id: int):
        body = await request.json() if await request.body() else {}
        run = state.workflows().run(workflow_id=wf_id, params=body.get("params") or {})
        return json_ok(request, {"ok": True, "data": run})

    @app.get("/api/workflows/runs")
    @app.get("/api/v1/workflows/runs")
    async def workflows_runs(request: Request):
        return json_ok(request, {"ok": True, "data": state.workflows().list_runs()})

    @app.get("/api/schedules")
    @app.get("/api/v1/schedules")
    async def schedules_list(request: Request):
        sched = state.scheduler()
        return json_ok(request, {"ok": True, "data": (sched.list() if sched else [])})

    @app.post("/api/schedules")
    @app.post("/api/v1/schedules")
    async def schedules_create(request: Request):
        sched = state.scheduler()
        if not sched:
            return json_err(request, "scheduler disabled", 400)
        body = await request.json() if await request.body() else {}
        s = sched.add(body.get("name", "sched"), body.get("kind", "daily"), body.get("value", ""),
                      int(body.get("workflow_id", 0)), body.get("params") or {})
        return json_ok(request, {"ok": True, "data": s}, 201)

    @app.patch("/api/schedules/{sched_id}")
    @app.patch("/api/v1/schedules/{sched_id}")
    async def schedules_patch(request: Request, sched_id: int):
        sc = state.scheduler()
        if not sc:
            return json_err(request, "scheduler disabled", 400)
        body = await request.json() if await request.body() else {}
        enabled = body.get("enabled")
        if enabled is None:
            return json_err(request, "enabled required")
        row = sc.set_enabled(sched_id, bool(enabled))
        return json_ok(request, {"ok": True, "data": row}) if row else json_err(request, "schedule not found", 404)

    @app.delete("/api/schedules/{sched_id}")
    @app.delete("/api/v1/schedules/{sched_id}")
    async def schedules_delete(request: Request, sched_id: int):
        sc = state.scheduler()
        if not sc:
            return json_err(request, "scheduler disabled", 400)
        sc.delete(sched_id)
        return json_ok(request, {"ok": True})

    # -- artifacts / uploads ----------------------------------------------
    @app.get("/api/artifacts/{artifact_id}/{filename}")
    @app.get("/api/v1/artifacts/{artifact_id}/{filename}")
    async def artifact_get(request: Request, artifact_id: str, filename: str):
        import tempfile
        from astra.core.artifacts import _safe_name
        artifact_dir = os.path.join(tempfile.gettempdir(), "astra", "artifacts")
        safe_filename = _safe_name(filename)
        safe_id = _safe_name(artifact_id)
        path = os.path.abspath(os.path.join(artifact_dir, f"{safe_id}_{safe_filename}"))
        root = os.path.abspath(artifact_dir)
        if not path.startswith(root + os.sep):
            return json_err(request, "forbidden", 403)
        if not os.path.isfile(path):
            return json_err(request, "artifact not found", 404)
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        return FileResponse(path, media_type=ctype,
                            headers={"Content-Disposition": f'inline; filename="{filename}"'})

    @app.get("/api/uploads/{filename}")
    @app.get("/api/v1/uploads/{filename}")
    async def upload_get(request: Request, filename: str):
        upload_dir = os.path.join(os.path.dirname(STATIC_DIR), "data", "uploads")
        path = os.path.abspath(os.path.join(upload_dir, filename))
        root = os.path.abspath(upload_dir)
        if not path.startswith(root + os.sep):
            return json_err(request, "forbidden", 403)
        if not os.path.isfile(path):
            return json_err(request, "file not found", 404)
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        return FileResponse(path, media_type=ctype)

    # -- ai providers / gateway -------------------------------------------
    @app.get("/api/providers")
    @app.get("/api/v1/providers")
    async def providers(request: Request):
        data = state.router().stats()
        provs = data.get("providers") or {}
        data["providers"] = {n: p for n, p in provs.items()
                             if p.get("credentials") and p.get("base_url") and p.get("models")}
        return json_ok(request, {"ok": True, "data": data})

    @app.get("/api/gateway/health")
    @app.get("/api/v1/gateway/health")
    async def gateway_health(request: Request):
        r = state.router()
        gw = r.gateway_health() if r is not None else {"state": "not_configured", "connections": []}
        return json_ok(request, {"ok": True, "data": gw})

    # -- agents / executions (orchestrator removed — kept for compat) ----
    @app.get("/api/agents")
    @app.get("/api/v1/agents")
    async def agents_list(request: Request):
        if state.orchestrator() is None:
            return json_ok(request, {"ok": True, "data": {"recent": [], "stats": {}}})
        return json_ok(request, {"ok": True, "data": {"recent": state.orchestrator().recent(),
                                                       "stats": state.orchestrator().stats()}})

    @app.post("/api/agents")
    @app.post("/api/v1/agents")
    async def agents_submit(request: Request):
        if state.orchestrator() is None:
            return json_err(request, "orchestrator removed — use POST /api/chat", 410)
        body = await request.json() if await request.body() else {}
        goal = (body.get("goal") or "").strip()
        if not goal:
            return json_err(request, "goal required")
        r = state.orchestrator().submit(goal, sync=bool(body.get("sync", False)))
        return json_ok(request, {"ok": True, "data": r}, 201)

    @app.get("/api/agents/{exec_id}")
    @app.get("/api/v1/agents/{exec_id}")
    async def agents_state(request: Request, exec_id: str):
        if state.orchestrator() is None:
            return json_err(request, "orchestrator removed — use POST /api/chat", 410)
        return json_ok(request, {"ok": True, "data": state.orchestrator().state(exec_id)})

    @app.post("/api/agents/{exec_id}/resume")
    @app.post("/api/v1/agents/{exec_id}/resume")
    async def agents_resume(request: Request, exec_id: str):
        if state.orchestrator() is None:
            return json_err(request, "orchestrator removed — use POST /api/chat", 410)
        body = await request.json() if await request.body() else {}
        return json_ok(request, {"ok": True, "data": state.orchestrator().resume(
            exec_id, bool(body.get("allow", True)))})

    @app.post("/api/agents/{exec_id}/cancel")
    @app.post("/api/v1/agents/{exec_id}/cancel")
    async def agents_cancel(request: Request, exec_id: str):
        if state.orchestrator() is None:
            return json_err(request, "orchestrator removed — use POST /api/chat", 410)
        return json_ok(request, {"ok": True, "data": state.orchestrator().cancel(exec_id)})

    @app.get("/api/executions")
    @app.get("/api/v1/executions")
    async def executions(request: Request):
        if state.orchestrator() is None:
            return json_ok(request, {"ok": True, "data": []})
        return json_ok(request, {"ok": True, "data": state.orchestrator().recent()})

    # -- /api/v1-only endpoints -------------------------------------------
    @app.get("/api/metrics")
    @app.get("/api/v1/metrics")
    async def metrics(request: Request):
        return json_ok(request, {"ok": True, "data": state.metrics()})

    @app.get("/api/models")
    @app.get("/api/v1/models")
    async def models(request: Request):
        reg = state._get("model_registry") or state._get("models")
        if reg is None:
            return json_err(request, "model registry unavailable", 400, "model_unavailable")
        router = state.router()
        health = router.health() if router else {}
        rows = []
        for m in reg.all_models():
            row = m.to_dict()
            row["status"] = health.get(m.provider, {}).get("status", "unknown")
            rows.append(row)
        summary = {"count": reg.count(),
                   "by_provider": {p: len(reg.for_provider(p)) for p in reg.providers()},
                   "by_status": reg.status_summary()}
        return json_ok(request, {"ok": True, "data": {"models": rows, "summary": summary}})

    @app.post("/api/models/refresh")
    @app.post("/api/v1/models/refresh")
    async def models_refresh(request: Request):
        disco = state._get("discovery")
        if disco is None:
            return json_err(request, "model discovery unavailable", 400, "model_unavailable")
        disco.refresh_all(force=True)
        return json_ok(request, {"ok": True, "data": {"refreshed": True}})

    @app.get("/api/router/status")
    @app.get("/api/v1/router/status")
    async def router_status(request: Request):
        return json_ok(request, {"ok": True, "data": state.router().health()})

    @app.get("/api/router/stats")
    @app.get("/api/v1/router/stats")
    async def router_stats(request: Request):
        r = state.router()
        return json_ok(request, {"ok": True, "data": {"routing": r.routing_stats(),
                                                       "task": r.task_stats(),
                                                       "last_route": r.last_route()}})

    @app.post("/api/providers/{name}/{action}")
    @app.post("/api/v1/providers/{name}/{action}")
    async def provider_admin(request: Request, name: str, action: str):
        if action not in ("refresh", "enable", "disable", "test", "reset-health"):
            return json_err(request, f"unknown action: {action}", 400)
        reg = state._get("provider_registry")
        router = state.router()
        if router is None or reg is None:
            return json_err(request, "providers unavailable", 400, "provider_unavailable")
        if action == "refresh":
            disco = state._get("discovery")
            if disco is not None:
                disco.discover(name, force=True)
            router.reset_health(name)
            return json_ok(request, {"ok": True, "data": {"provider": name, "refreshed": True}})
        if action in ("enable", "disable"):
            want = action == "enable"
            router.enable(name) if want else router.disable(name)
            return json_ok(request, {"ok": True, "data": {"provider": name, "enabled": want}})
        if action == "test":
            result = router.test_provider(name)
            return json_ok(request, {"ok": True, "data": result})
        if action == "reset-health":
            router.reset_health(name)
            info = router.health().get(name, {})
            return json_ok(request, {"ok": True, "data": {
                "provider": name, "calls": info.get("calls", 0), "errors": info.get("errors", 0),
                "state": info.get("state"), "healthy": info.get("healthy")}})

    @app.post("/api/providers/{name}/test/{model_id}")
    @app.post("/api/v1/providers/{name}/test/{model_id}")
    async def provider_model_test(request: Request, name: str, model_id: str):
        router = state.router()
        if router is None:
            return json_err(request, "providers unavailable", 400, "provider_unavailable")
        key_id = (request.query_params.get("key") or "").strip() or None
        result = router.test_provider_model(name, model_id, key_id)
        return json_ok(request, {"ok": True, "data": result})

    @app.post("/api/providers/test-all")
    @app.post("/api/v1/providers/test-all")
    async def providers_test_all(request: Request):
        router = state.router()
        if router is None:
            return json_err(request, "providers unavailable", 400, "provider_unavailable")
        router.reset_all_health()
        providers_r = router.test_all_providers()
        gw = getattr(router, "gateway", None)
        connections = gw.test_all_connections() if gw is not None else []
        return json_ok(request, {"ok": True, "data": {"providers": providers_r, "connections": connections}})

    @app.post("/api/gateway/test")
    @app.post("/api/v1/gateway/test")
    async def gateway_test(request: Request):
        router = state.router()
        gw = getattr(router, "gateway", None) if router else None
        if gw is None:
            return json_err(request, "Astra AI Gateway not configured", 400, "gateway_unavailable")
        return json_ok(request, {"ok": True, "data": gw.test_all_connections()})

    @app.post("/api/gateway/{name}/test")
    @app.post("/api/v1/gateway/{name}/test")
    async def gateway_test_one(request: Request, name: str):
        router = state.router()
        gw = getattr(router, "gateway", None) if router else None
        if gw is None:
            return json_err(request, "Astra AI Gateway not configured", 400, "gateway_unavailable")
        return json_ok(request, {"ok": True, "data": gw.test_connection_by_name(name)})

    @app.post("/api/gateway/{name}/test/{model_id}")
    @app.post("/api/v1/gateway/{name}/test/{model_id}")
    async def gateway_model_test(request: Request, name: str, model_id: str):
        router = state.router()
        gw = getattr(router, "gateway", None) if router else None
        if gw is None:
            return json_err(request, "Astra AI Gateway not configured", 400, "gateway_unavailable")
        result = gw.test_connection_model_by_name(name, model_id)
        return json_ok(request, {"ok": True, "data": result})

    # -- web3 --------------------------------------------------------------
    @app.get("/api/web3/transactions")
    @app.get("/api/v1/web3/transactions")
    async def web3_tx_list(request: Request):
        tx = state._get("tx_manager")
        if tx is None:
            return json_err(request, "Web3 transaction manager unavailable", 400, "web3_unavailable")
        return json_ok(request, {"ok": True, "data": {"transactions": tx.list(100), "stats": tx.stats()}})

    @app.get("/api/web3/transactions/{tx_id}")
    @app.get("/api/v1/web3/transactions/{tx_id}")
    async def web3_tx_get(request: Request, tx_id: str):
        tx = state._get("tx_manager")
        if tx is None:
            return json_err(request, "Web3 transaction manager unavailable", 400, "web3_unavailable")
        row = tx.get(tx_id)
        if row is None or row.get("status") == "UNKNOWN":
            return json_err(request, "transaction not found", 404, "transaction_not_found")
        return json_ok(request, {"ok": True, "data": row})

    @app.get("/api/web3/transaction-policy")
    @app.get("/api/v1/web3/transaction-policy")
    async def web3_policy(request: Request):
        policy = state._get("web3_policy") or state._get("tx_policy")
        if policy is None:
            return json_err(request, "transaction policy unavailable", 400, "web3_unavailable")
        data = {"policy": policy.describe(), "mode": policy.mode,
                "stopped": bool(getattr(state._get("tx_manager"), "stopped", False))}
        return json_ok(request, {"ok": True, "data": data})

    @app.post("/api/web3/transaction-policy/mode")
    @app.post("/api/v1/web3/transaction-policy/mode")
    async def web3_policy_mode(request: Request):
        if not state.operator_token:
            return json_err(request, "set ASTRA_TOKEN to allow Web3 mode changes", 403, "authorization")
        policy = state._get("web3_policy") or state._get("tx_policy")
        tx = state._get("tx_manager")
        if policy is None or tx is None:
            return json_err(request, "transaction policy unavailable", 400, "web3_unavailable")
        body = await request.json() if await request.body() else {}
        mode = (body.get("mode") or "").strip().upper()
        if mode not in ("CONFIRM", "AUTO"):
            return json_err(request, "mode must be CONFIRM or AUTO", 400, "validation")
        policy.set_mode(mode)
        return json_ok(request, {"ok": True, "data": {"mode": policy.mode}})

    @app.post("/api/web3/transactions/{tx_id}/{action}")
    @app.post("/api/v1/web3/transactions/{tx_id}/{action}")
    async def web3_tx_action(request: Request, tx_id: str, action: str):
        if action not in ("authorize", "reject"):
            return json_err(request, "unknown action", 404)
        if not state.operator_token:
            return json_err(request, "set ASTRA_TOKEN to allow Web3 transaction approval", 403, "authorization")
        tx = state._get("tx_manager")
        if tx is None:
            return json_err(request, "Web3 transaction manager unavailable", 400, "web3_unavailable")
        from astra.web3.policy import (TransactionPolicyError, TransactionRejectedError,
                                       TransactionFailedError)
        body = await request.json() if await request.body() else {}
        try:
            if action == "reject":
                reason = (body or {}).get("reason") or "operator rejected"
                rec = tx.reject(tx_id, reason=reason)
            else:
                tx.authorize(tx_id)
                rec = tx.sign_and_broadcast(tx_id)
            return json_ok(request, {"ok": True, "data": rec})
        except (TransactionPolicyError, TransactionRejectedError, TransactionFailedError) as exc:
            return json_err(request, str(exc), 400, "transaction_policy")

    # -- catch-all: unknown /api/* routes -> 404 with the old error shape --
    @app.api_route("/api/{full_path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
    async def api_catch_all(request: Request, full_path: str):
        return json_err(request, "unknown route", 404)

    # -- SPA fallback for any other path (client-side routing) ------------
    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        return _serve_static(full_path)

    return app
