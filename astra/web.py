"""Astra web layer: the router, plus the app that serves it.

This module owns everything that decides *what* an HTTP request means — the
route table, operator auth, rate limiting, CORS, security headers, secret
redaction, uploads, the body cap and the SSE live feed. It is deliberately
free of sockets and frameworks: it deals in `Request`/`Response` objects, and
`WebApp.handle()` is a pure function of the request plus the `AstraSite`. That
is what lets the entire API be unit-tested with no server running at all.

`astra/web_fastapi.py` owns *how* those bytes travel — it binds this router to
FastAPI/uvicorn (one catch-all route, a lifespan that builds and tears down the
stack, and a worker threadpool for blocking work), and `run.py` is the
launcher. There is exactly one server: FastAPI/ASGI.

Security hardening (production build):
  * optional operator auth — set ASTRA_TOKEN: all /api/* then require a
    Bearer token / X-Astra-Token header / ?token= (SSE has no headers);
    unset token = open (safe local-first default)
  * per-IP rate limiting — ASTRA_API_RATE_LIMIT (default 300 requests/min)
  * request body cap — ASTRA_MAX_BODY_MB (default 50 MB)
  * security headers + Content-Security-Policy on every response
  * X-Request-Id on every response; request_id in every JSON body
  * structured JSON errors {ok, error, error_code, request_id} — never
    leaks stack traces (message detail gated to non-production)
  * every response redacted through security.redact — no secrets egress

Versioning: every /api/... route is ALSO served under /api/v1/... (the v1
segment is stripped before dispatch, so there is no duplicated code path).
New endpoints exist only under /api/v1.

Core endpoints (all old routes stay backward-compatible):

  GET  /api/manifest        agent name + tab manifest for the SPA
  POST /api/chat            {message} -> agent reply {reply, action, data, ok}
  GET  /api/chat/history[?after_id=N][&conversation_id=N]  saved transcript
                             (defaults to the CURRENT chat) + {pending}
                             scoped to that chat (DELETE wipes every chat)
  GET  /api/chat/conversations         list saved chats {id, title, count,
                             updated_at, current} newest-first
  POST /api/chat/conversations         open a new chat, becomes current
                             (reuses the current one if it's still empty)
  GET  /api/chat/conversations/<id>    switch current chat + its history
  DELETE /api/chat/conversations/<id>  delete one chat
  POST /api/chat/resume     {execution_id, allow} -> legacy approve/reject
                             round-trip; the orchestrator that owned it was
                             removed, so this answers honestly that there is
                             nothing to resume (same reply shape as /api/chat)
  GET  /api/dashboard       dashboard blocks (currently empty placeholder)
  GET  /api/export          export everything (currently a placeholder)
  POST /api/import          import everything (currently a placeholder)

System endpoints:

  GET  /api/health          database/providers/tools/scheduler diagnostics
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
  GET  /api/gateway/health  Astra AI Gateway status (4 connections + fallback)
  GET  /api/agents          legacy execution routes — the orchestrator was
                             removed, so these return an empty list / 410
  GET  /api/executions      alias for /api/agents (same legacy behaviour)

New /api/v1 endpoints:

  GET  /api/v1/models            model registry (capabilities + health)
  POST /api/v1/models/refresh    re-run provider model discovery
  GET  /api/v1/router/status     routing health
  GET  /api/v1/router/stats      routing + task statistics
  POST /api/v1/providers/<name>/refresh | enable | disable | test
  POST /api/v1/providers/<name>/test/<model>[?key=<key_id>]  test one model only
                                     (optionally via one specific API key); result
                                     returned as soon as that one call ends
  POST /api/v1/providers/test-all   test every provider + every Astra AI
                                     Gateway connection in one call (each
                                     result saved as soon as it completes)
  POST /api/v1/gateway/test         test only the 4 Astra AI Gateway
                                     connections (Gemini/Groq/Cloudflare/
                                     Bedrock) — every model of each
  POST /api/v1/gateway/<name>/test  test one Gateway connection only —
                                     every model that connection exposes
  GET  /api/v1/web3/transactions              (+ /{tx_id})
  GET  /api/v1/web3/transaction-policy        (mode/limits/whitelist/stop)
  POST /api/v1/web3/transaction-policy/mode   (operator token required)
  POST /api/v1/web3/transactions/{tx_id}/authorize  (CONFIRM-mode approve; operator token required)
  POST /api/v1/web3/transactions/{tx_id}/reject     (CONFIRM-mode reject; operator token required)
  GET  /api/metrics              server + subsystem metrics
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
import time
from urllib.parse import urlparse, parse_qs, unquote

from .agent import Agent
from .chat_log import ChatLog
from .ai.conversation_context import ConversationContextBuilder
from .security import (ApiError, RateLimiter, make_request_id, redact)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


def uploads_dir(cfg=None) -> str:
    """Absolute uploads directory, under the same DATA_DIR as the database.

    Uploads used to be hardcoded to `<repo>/data/uploads`, so a deployment
    that relocated DATA_DIR (a container volume, /sdcard on Termux) kept its
    database there but wrote uploaded files into the install directory.
    Honouring DATA_DIR keeps them together (and survives a read-only app
    dir)."""
    data_dir = ""
    if cfg is not None:
        data_dir = cfg.get("DATA_DIR", "") or ""
    data_dir = data_dir or os.environ.get("DATA_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    return os.path.join(data_dir, "uploads")


AGENT_NAME = "Astra AI Agent"

CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "js": "application/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp",
    "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
    "m4a": "audio/mp4", "aac": "audio/aac", "flac": "audio/flac",
    "opus": "audio/opus",
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    "pdf": "application/pdf",
    "csv": "text/csv", "tsv": "text/tab-separated-values",
    "txt": "text/plain; charset=utf-8", "md": "text/plain; charset=utf-8",
    "xml": "application/xml", "yaml": "application/x-yaml",
    "zip": "application/zip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

CORE_TABS = [
    {"tab": "dashboard", "label": "\U0001f4ca Dashboard", "core": True},
    {"tab": "assistant", "label": "\U0001f916 Assistant", "core": True},
    {"tab": "providers", "label": "\U0001f50c AI Providers health", "core": True},
    {"tab": "router", "label": "\U0001f9e0 Router", "core": True},
    {"tab": "web3", "label": "\u26d3\ufe0f Wallet", "core": True},
    {"tab": "backup", "label": "\U0001f4be Backup", "core": True},
]

# Always appended after the core tabs — see AstraSite.manifest().
LOGS_TAB = {"tab": "logs", "label": "\U0001f4e1 Activity Log", "core": True}

# Header templates for a hardened server.
# * X-Frame-Options / nosniff / Referrer-Policy / CSP defend the SPA
# * X-Request-Id correlates one request across logs + response bodies
# * Cache-Control: no-store keeps API payloads off shared caches
SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("Referrer-Policy", "no-referrer"),
    ("X-XSS-Protection", "0"),  # modern browsers: this header is deprecated
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Content-Security-Policy",
     "default-src 'self'; img-src 'self' data: blob:; "
     "media-src 'self' blob:; "
     "style-src 'self' 'unsafe-inline'; script-src 'self'; "
     "connect-src 'self'; frame-ancestors 'self'"),
]

CORS_METHODS = "GET,POST,PATCH,DELETE,OPTIONS"
CORS_HEADERS = "Content-Type, X-Astra-Token, Authorization"


# ── request / response value objects ─────────────────────────────────────────

class Headers(dict):
    """Case-insensitive header mapping (keys are stored lower-case)."""

    def __init__(self, items=None):
        super().__init__()
        if items:
            pairs = items.items() if hasattr(items, "items") else items
            for key, value in pairs:
                self[key] = value

    def __setitem__(self, key, value):
        super().__setitem__(str(key).lower(), value)

    def __contains__(self, key):
        return super().__contains__(str(key).lower())

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)


class Request:
    """One HTTP request, already parsed — no socket, no framework."""

    def __init__(self, method, raw_path, headers=None, body=None,
                 fields=None, files=None, remote_ip="unknown", rid=""):
        self.method = (method or "GET").upper()
        self.raw_path = raw_path or "/"
        self.path = split_path(self.raw_path)
        self.query = parse_query(self.raw_path)
        self.headers = headers if isinstance(headers, Headers) else Headers(headers)
        self.body = body if isinstance(body, dict) else {}
        self.fields = fields or {}
        self.files = files or []
        self.remote_ip = remote_ip or "unknown"
        self.rid = rid or make_request_id()


class Response:
    """One HTTP response, ready to be sent by the ASGI layer.

    `stream` (an async iterator of bytes — the SSE feed) wins over `body`
    when set: the caller then omits Content-Length and writes frames as they
    are produced.
    """

    __slots__ = ("status", "body", "stream", "content_type",
                 "headers", "cache", "cors", "hardened")

    def __init__(self, status=200, body=b"", content_type=None, headers=None,
                 stream=None, cache="no-store", cors=False, hardened=True):
        self.status = status
        self.body = body if body is not None else b""
        self.stream = stream
        self.content_type = content_type
        self.headers = list(headers or [])
        self.cache = cache
        self.cors = cors
        self.hardened = hardened

    @property
    def has_stream(self) -> bool:
        return self.stream is not None


def json_response(payload, status=200, rid="", cors=True):
    """Serialise a payload as JSON with full hardening. The payload is
    redacted defensively so a subsystem that leaks a secret can never egress
    it through the API. The request_id rides both header and body."""
    if isinstance(payload, dict) and "request_id" not in payload:
        payload = {**payload, "request_id": rid}
    body = json.dumps(redact(payload), ensure_ascii=False).encode("utf-8")
    return Response(status, body, "application/json; charset=utf-8", cors=cors)


def error_response(message, status=400, error_code="bad_request", rid=""):
    """Structured error envelope — no stack traces, ever."""
    return json_response({"ok": False, "error": message,
                          "error_code": error_code, "request_id": rid},
                         status, rid)


def int_arg(value, default=None, *, name="value"):
    """Parse a client-supplied integer.

    Raises ApiError(400) on garbage instead of letting `int()` raise a
    ValueError that the generic handler turns into an opaque 500 — a bad
    query string is a client error, not a server fault. `None`/`""` yields
    `default` (so an omitted parameter keeps its documented fallback)."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError("bad_request", f"{name} must be an integer")


def api_error_response(exc, rid=""):
    """ApiError -> response, preserving the status/code it carries."""
    if not exc.request_id:
        exc.request_id = rid
    return json_response(exc.to_dict(), exc.status, rid)


# Statuses that must not carry a body (nor a Content-Length).
_NO_BODY_STATUS = (204, 304)


def response_headers(resp, req, site, method=None):
    """The complete, ordered header list for a response. Both adapters call
    this and nothing else, so the two servers emit byte-identical headers."""
    method = (method or req.method).upper()
    out = []
    if resp.content_type:
        out.append(("Content-Type", resp.content_type))
    if not resp.has_stream and resp.status >= 200 and resp.status not in _NO_BODY_STATUS:
        out.append(("Content-Length", str(len(resp.body))))
    if resp.cache:
        out.append(("Cache-Control", resp.cache))
    out.append(("X-Request-Id", req.rid))
    if resp.hardened:
        out.extend(SECURITY_HEADERS)
    if resp.cors:
        origin = site.cors_origin(req.headers.get("Origin"))
        if origin:
            out.append(("Access-Control-Allow-Origin", origin))
            out.append(("Vary", "Origin"))
            if method == "OPTIONS":
                out.append(("Access-Control-Allow-Methods", CORS_METHODS))
                out.append(("Access-Control-Allow-Headers", CORS_HEADERS))
                out.append(("Access-Control-Max-Age", "600"))
    out.extend(resp.headers)
    return out


def check_body_size(site, content_length) -> "Response | None":
    """Enforce the request body cap before any bytes are buffered. Returns an
    error response to send, or None when the body is acceptable."""
    try:
        length = int(content_length or 0)
    except (TypeError, ValueError):
        length = 0
    if length > site.max_body_bytes:
        return error_response(
            "payload_too_large",
            status=413, error_code="payload_too_large")
    return None


# ── parsing helpers ──────────────────────────────────────────────────────────

def split_path(raw_path) -> list:
    """Split on the *raw* "/" first, then percent-decode each segment, so an
    encoded model id like "openai%2Fgpt-oss-120b" or "%40cf%2Fmeta%2F..."
    arrives as one segment with its real "/" and "@" restored."""
    return [unquote(p) for p in urlparse(raw_path).path.split("/") if p]


def parse_query(raw_path) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(raw_path).query).items()}


def resolve_v1(path: list) -> list:
    """/api/v1/<rest> -> /api/<rest> so every legacy route is served under
    the versioned prefix with no duplicated code path."""
    if len(path) >= 3 and path[0] == "api" and path[1] == "v1":
        return ["api"] + path[2:]
    return path


def route_matcher(route_parts, path_parts):
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


def parse_multipart_body(raw: bytes, content_type: str) -> tuple:
    """Minimal multipart/form-data parser (stdlib `cgi` was removed in
    Python 3.13, so this replaces `cgi.FieldStorage` for our purposes).

    Returns (fields_dict, files_list) where each file is
    {filename, data (bytes), content_type}."""
    fields: dict = {}
    files: list = []
    if not raw or "boundary=" not in content_type:
        return fields, files

    boundary = content_type.split("boundary=", 1)[1].strip()
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]
    boundary = boundary.encode("utf-8")
    delimiter = b"--" + boundary
    # Split on the delimiter; drop preamble/epilogue and the closing "--".
    parts = raw.split(delimiter)
    for part in parts:
        if not part or part in (b"--", b"--\r\n"):
            continue
        # Each part looks like: \r\n<headers>\r\n\r\n<body>\r\n
        part = part.strip(b"\r\n")
        if not part:
            continue
        if b"\r\n\r\n" in part:
            header_block, body = part.split(b"\r\n\r\n", 1)
        elif b"\n\n" in part:
            header_block, body = part.split(b"\n\n", 1)
        else:
            continue
        # Trailing CRLF before the next boundary belongs to the delimiter.
        if body.endswith(b"\r\n"):
            body = body[:-2]
        elif body.endswith(b"\n"):
            body = body[:-1]

        headers = {}
        for line in header_block.split(b"\r\n"):
            if b":" not in line:
                continue
            name, _, value = line.partition(b":")
            headers[name.strip().lower().decode("latin-1")] = \
                value.strip().decode("latin-1")

        disposition = headers.get("content-disposition", "")
        if "form-data" not in disposition:
            continue
        field_name = None
        filename = None
        for piece in disposition.split(";"):
            piece = piece.strip()
            if piece.startswith("name="):
                field_name = piece[len("name="):].strip('"')
            elif piece.startswith("filename="):
                filename = piece[len("filename="):].strip('"')
        if field_name is None:
            continue

        if filename:
            files.append({
                "filename": filename,
                "data": body,
                "content_type": headers.get("content-type",
                                            "application/octet-stream"),
            })
        else:
            fields[field_name] = body.decode("utf-8", errors="replace")

    return fields, files


def content_type_for(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return CONTENT_TYPES.get(ext, "application/octet-stream")


# ── the site: everything a request needs, minus the socket ────────────────────

class AstraSite:
    """Dependency container + config knobs + in-memory metrics.

    This is the object the route table calls into; it is deliberately not tied
    to a server, so `make_app()` hands one straight to FastAPI and the tests
    can drive the router with no server at all.
    """

    def __init__(self, addr, store, agent: Agent, stack=None):
        self.store = store
        self.agent = agent
        self.chat_log = ChatLog(store, redact=redact)
        self._stack = stack or {}
        cfg = self._get("config")
        # Single source of prior-turn context for /api/chat (see
        # astra/ai/conversation_context.py): reads the SAME ChatLog every
        # turn is persisted to, so the Gateway and the Provider are always
        # shown the real, isolated-per-conversation transcript — never
        # whatever (if anything) a client happened to send.
        self.context_builder = ConversationContextBuilder(
            self.chat_log,
            max_chars=(cfg.getint("CHAT_CONTEXT_MAX_CHARS", 6000) if cfg else 6000),
            max_turns=(cfg.getint("CHAT_CONTEXT_MAX_TURNS", 20) if cfg else 20))
        # -- security knobs (all safe defaults) --------------------------------
        self.env = (cfg.get("ENV") if cfg else None) or os.environ.get("ENV", "development")
        self.operator_token = (cfg.get("ASTRA_TOKEN") if cfg else None) \
            or os.environ.get("ASTRA_TOKEN") or os.environ.get("ASTRA_API_KEY", "")
        self.max_body_bytes = int((cfg.getint("ASTRA_MAX_BODY_MB", 50)
                                   if cfg else 50) * 1024 * 1024)
        try:
            rl = (cfg.getint("ASTRA_API_RATE_LIMIT", 300) if cfg else 300)
        except Exception:
            rl = 300
        self.rate_limiter = RateLimiter(rl, 60.0)
        self._allowed_origins = set(
            (cfg.getlist("ASTRA_CORS_ORIGINS") if cfg else None) or [])
        # -- observability: tiny in-memory request counters --------------------
        self._req_lock = threading.Lock()
        self._req = {"count": 0, "errors": 0,
                     "by_path": {}, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._metrics_start = time.time()

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

    # -- security / observability -------------------------------------------
    def cors_origin(self, origin):
        """CORS: explicit ASTRA_CORS_ORIGINS wins; else same-origin always,
        cross-origin allowed for SPA/LAN tools unless production."""
        if not origin:
            return None
        if self._allowed_origins:
            if "*" in self._allowed_origins:
                return "*"
            return origin if origin in self._allowed_origins else None
        if self.env == "production":
            return None  # same-origin only
        return origin  # dev/LAN: reflect request origin

    def note_request(self, method, path, error=False):
        with self._req_lock:
            self._req["count"] += 1
            if error:
                self._req["errors"] += 1
            label = "/".join(path) if path else "/"
            blob = self._req["by_path"].setdefault(label, {"count": 0, "errors": 0})
            blob["count"] += 1
            if error:
                blob["errors"] += 1

    def metrics(self) -> dict:
        """/api/metrics aggregate — secret-free, cheap, always available."""
        out = {"app": AGENT_NAME,
               "uptime_s": round(time.time() - self._metrics_start, 1),
               "requests": dict(self._req)}
        if self.router():
            out["router"] = {name: {k: info[k] for k in
                                    ("healthy", "state", "calls",
                                     "errors", "latency_avg_ms")
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
            out["orchestrator"] = {k: v for k, v in orch.stats().items()
                                   if not isinstance(v, dict)}
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
        # NOTE: the plugin system has been removed — "plugins" is always [].
        out["checks"]["plugins"] = []
        if self.router():
            out["checks"]["providers"] = self.router().health()
        if self.scheduler():
            out["checks"]["scheduler"] = self.scheduler().stats()
        # unconfigured providers report "not configured", not unhealthy — a
        # missing API key must not flip /api/health red
        provider_unhealthy = any(
            p.get("healthy") is False
            for name, p in out["checks"].get("providers", {}).items())
        if provider_unhealthy:
            out["ok"] = False
        return out

    def registry_plugin_tools(self):
        """NOTE: the plugin system has been removed — no-op kept so any
        remaining callers don't break."""
        return

    def manifest(self) -> dict:
        """Frontend bootstrap: agent name + always-on core tabs.

        NOTE: the plugin system has been removed — no plugin tabs/entries
        are added anymore (plugins/ is an empty placeholder)."""
        tabs = [dict(t) for t in CORE_TABS]
        tabs.append(dict(LOGS_TAB))
        return {
            "name": AGENT_NAME,
            "version": getattr(__import__("astra"), "__version__", "1.0.0"),
            "plugins": [],
            "tabs": tabs,
        }

    def public_config(self) -> dict:
        return self.cfg().all()


# ── the router ───────────────────────────────────────────────────────────────

class WebApp:
    """The single route table. Adapters construct one per site and call
    `handle()` for every request — nothing else decides anything."""

    def __init__(self, site: AstraSite):
        self.site = site

    # -- gate ----------------------------------------------------------------
    def _authorized(self, req: Request) -> bool:
        """Operator token gate. ASTRA_TOKEN unset = open (local-first);
        set = Bearer / X-Astra-Token / ?token= must match, constant-time."""
        token = self.site.operator_token
        if not token:
            return True
        supplied = req.headers.get("Authorization", "")
        if supplied.lower().startswith("bearer "):
            candidate = supplied[7:].strip()
        else:
            candidate = (req.headers.get("X-Astra-Token", "")
                         or req.query.get("token", ""))
        return bool(candidate) and hmac.compare_digest(candidate, token)

    def _rate_limited(self, req: Request) -> bool:
        limiter = self.site.rate_limiter
        if not limiter:
            return False
        return not limiter.allow(req.remote_ip)

    def _gate(self, req: Request, path: list):
        """Auth + rate-limit for API traffic. Returns a response to send, or
        None to proceed."""
        if not (path and path[0] == "api"):
            return None
        if not self._authorized(req):
            return error_response("authentication required", 401,
                                  "authentication", req.rid)
        if self._rate_limited(req):
            return error_response("rate limit exceeded", 429,
                                  "rate_limit", req.rid)
        return None

    def options(self, req: Request) -> Response:
        """CORS preflight: 204 with the same hardening as any other reply."""
        return Response(204, cache=None, cors=True)

    # -- entry point ---------------------------------------------------------
    def handle(self, req: Request) -> Response:
        site = self.site
        path = resolve_v1(req.path)
        gated = self._gate(req, path)
        if gated is not None:
            return gated
        site.note_request(req.method, path)
        try:
            return self._route(req, path)
        except ApiError as e:
            site.note_request(req.method, path, error=True)
            return api_error_response(e, req.rid)
        except Exception as e:
            site.note_request(req.method, path, error=True)
            detail = str(e) if site.env != "production" else ""
            msg = f"internal error{': ' + detail if detail else ''}"
            return error_response(msg, 500, "internal", req.rid)

    # -- static / stored files ----------------------------------------------
    def _static(self, rel: str, req: Request) -> Response:
        if rel in ("", "/"):
            rel = "index.html"
        rel = rel.lstrip("/")
        if ".." in rel or "\\" in rel:
            return error_response("bad path", 400, "bad_request", req.rid)
        root = os.path.abspath(STATIC_DIR)
        path = os.path.abspath(os.path.join(root, rel))
        # path-traversal guard: resolved path must stay inside static root
        if not (path == root or path.startswith(root + os.sep)):
            return error_response("forbidden", 403, "bad_request", req.rid)
        if not os.path.isfile(path):
            # plain text, not an HTML error template: this is a JSON API and
            # the SPA renders its own 404
            return Response(404, b"not found", "text/plain; charset=utf-8",
                            cache=None)
        with open(path, "rb") as fh:
            body = fh.read()
        # No cache header here meant the browser was free to keep serving a
        # stale astra.js/style.css indefinitely after every deploy — a UI
        # fix landing in git often wouldn't actually reach the page until a
        # manual hard-refresh. These are cheap to re-fetch and change on
        # every deploy, so always make the browser revalidate them.
        cache = ("no-cache, must-revalidate"
                 if path.endswith((".html", ".js", ".css")) else None)
        return Response(200, body, content_type_for(path), cache=cache)

    def _serve_upload(self, filename: str, req: Request) -> Response:
        upload_dir = uploads_dir(self.site.cfg())
        path = os.path.abspath(os.path.join(upload_dir, filename))
        root = os.path.abspath(upload_dir)
        if not path.startswith(root + os.sep):
            return error_response("forbidden", 403, "bad_request", req.rid)
        if not os.path.isfile(path):
            return error_response("file not found", 404, "bad_request", req.rid)
        with open(path, "rb") as fh:
            body = fh.read()
        return Response(200, body, content_type_for(path), cache=None)

    def _serve_artifact(self, artifact_id: str, filename: str,
                        req: Request) -> Response:
        """Serve a stored artifact file.

        Generated artifacts are written by astra.core.artifacts.store_artifact
        into `{tempdir}/astra/artifacts/{id}_{safe_filename}` (a flat
        directory, id-prefixed — see generate_document / agent.py). This must
        match that exact scheme or every download 404s.
        """
        import tempfile
        from .core.artifacts import _safe_name

        artifact_dir = os.path.join(tempfile.gettempdir(), "astra", "artifacts")
        safe_filename = _safe_name(filename)
        safe_id = _safe_name(artifact_id)
        path = os.path.abspath(
            os.path.join(artifact_dir, f"{safe_id}_{safe_filename}"))
        root = os.path.abspath(artifact_dir)
        if not path.startswith(root + os.sep):
            return error_response("forbidden", 403, "bad_request", req.rid)
        if not os.path.isfile(path):
            return error_response("artifact not found", 404, "bad_request",
                                  req.rid)
        with open(path, "rb") as fh:
            body = fh.read()
        return Response(200, body, content_type_for(path), cache=None,
                        headers=[("Content-Disposition",
                                  f'inline; filename="{filename}"')])

    def _process_uploads(self, files: list) -> list:
        """Process uploaded files into attachment dicts for the agent."""
        if not files:
            return []
        upload_dir = uploads_dir(self.site.cfg())
        os.makedirs(upload_dir, exist_ok=True)
        attachments = []
        for f in files[:10]:
            try:
                from .core.attachments import process_upload
                att = process_upload(f["data"], f["filename"], upload_dir)
                attachments.append(att.to_dict())
            except Exception as e:
                attachments.append({
                    "filename": f.get("filename", "unknown"),
                    "error": str(e),
                    "processed": False,
                })
        return attachments

    # -- live events (SSE) ---------------------------------------------------
    def _sse(self, req: Request) -> Response:
        """Server-Sent Events feed — tails new events, writing `data:` frames
        so the browser Live tab streams activity with no WebSocket dependency.
        Driven by an async generator, so it never holds a worker thread."""
        return Response(
            200,
            content_type="text/event-stream; charset=utf-8",
            stream=sse_frames(self.site, req),
            cache="no-cache",
            cors=True,
            headers=[("Connection", "keep-alive")],
        )

    # -- main route table ----------------------------------------------------
    def _route(self, req: Request, path: list) -> Response:
        site, q, body = self.site, req.query, req.body
        method = req.method

        if not path:
            return self._static("index.html", req)
        if path[0] == "static":
            return self._static("/".join(path[1:]), req)
        if path[0] == "favicon.ico":
            return Response(204, cache=None, hardened=False)

        # system / manifest endpoints
        if path == ["api", "manifest"]:
            return json_response({"ok": True, "data": site.manifest()}, rid=req.rid)
        if path == ["api", "chat"] and method == "POST":
            # The user message is saved BEFORE the agent runs and the reply
            # is saved the moment it exists — so a page refresh mid-turn
            # loses nothing: the reloaded page reads it back from
            # /api/chat/history (see astra/chat_log.py).
            log = site.chat_log
            # Pin this whole turn — history lookup, the message write, and
            # the reply write — to the chat the message actually landed in.
            # `current_id` can change while the agent is still working (the
            # user switches chats or opens a new one); none of that may
            # move this turn to a different conversation.
            cid = log.current_id
            # Build the canonical conversation history BEFORE the current
            # message is persisted, from THIS conversation only (never
            # another one), so it can never include — let alone duplicate —
            # the message the user just sent. See
            # astra/ai/conversation_context.py.
            history = site.context_builder.build(cid)
            if "multipart/form-data" in (req.headers.get("Content-Type") or ""):
                fields, files = req.fields, req.files
                msg = fields.get("message", "")
                names = [f.get("filename", "file") for f in (files or [])[:10]]
                attachments = self._process_uploads(files)
                if log.is_duplicate_pending(cid, msg):
                    # A refresh/retry resubmitted the exact message whose
                    # turn is still running — do not start a second agent
                    # run (and a second persisted reply) for it. The
                    # in-flight turn already covers this message; the
                    # client should keep polling /api/chat/history.
                    reply = {"reply": "", "action": "none", "ok": True,
                            "data": {"duplicate_of_pending": True}}
                else:
                    cid = log.add_user(msg, files=names, conversation_id=cid)
                    token = log.begin(cid)
                    try:
                        reply = site.agent.handle(
                            msg, history=history, attachments=attachments or None)
                        log.add_reply(reply, conversation_id=cid)
                    finally:
                        log.end(token)
            else:
                msg = body.get("message", "")
                if log.is_duplicate_pending(cid, msg):
                    reply = {"reply": "", "action": "none", "ok": True,
                            "data": {"duplicate_of_pending": True}}
                else:
                    cid = log.add_user(msg, conversation_id=cid)
                    token = log.begin(cid)
                    try:
                        reply = site.agent.handle(msg, history=history)
                        log.add_reply(reply, conversation_id=cid)
                    finally:
                        log.end(token)
            # The browser may have switched to a different chat (or
            # opened a new one) while this was running — tell it which
            # chat this reply actually belongs to, so it only paints the
            # bubble into the log if that's still what's on screen.
            reply = dict(reply)
            reply["conversation_id"] = cid
            return json_response({"ok": True, "data": reply}, rid=req.rid)
        if path == ["api", "chat", "history"] and method == "GET":
            cid_param = q.get("conversation_id")
            cid = int_arg(cid_param, name="conversation_id") if cid_param else None
            return json_response({"ok": True, "data": site.chat_log.history(
                after_id=int_arg(q.get("after_id"), 0, name="after_id"),
                limit=min(int_arg(q.get("limit"), 200, name="limit"), 500),
                conversation_id=cid)}, rid=req.rid)
        if path == ["api", "chat", "history"] and method == "DELETE":
            return json_response({"ok": True,
                                  "removed": site.chat_log.clear()}, rid=req.rid)
        # chat history: the trash/menu pair in the Assistant tab. Each chat is a
        # "conversation"; GET lists them, POST opens a new one (reusing
        # the current chat if it's still empty), GET/DELETE on an id
        # switches to / deletes that particular chat.
        if path == ["api", "chat", "conversations"] and method == "GET":
            return json_response({"ok": True,
                                  "data": site.chat_log.list_conversations()},
                                 rid=req.rid)
        if path == ["api", "chat", "conversations"] and method == "POST":
            cid = site.chat_log.new_conversation()
            return json_response({"ok": True, "data": {"id": cid}}, rid=req.rid)
        if (len(path) == 4 and path[:3] == ["api", "chat", "conversations"]
                and path[3].isdigit()):
            cid = int(path[3])
            if method == "GET":
                if not site.chat_log.switch(cid):
                    return error_response("chat not found", 404, "bad_request",
                                          req.rid)
                return json_response({"ok": True,
                                      "data": site.chat_log.history(
                                          conversation_id=cid)}, rid=req.rid)
            if method == "DELETE":
                if not site.chat_log.delete_conversation(cid):
                    return error_response("chat not found", 404, "bad_request",
                                          req.rid)
                return json_response({"ok": True,
                                      "data": {"current": site.chat_log.current_id}},
                                     rid=req.rid)
        if path == ["api", "chat", "resume"] and method == "POST":
            eid = (body.get("execution_id") or "").strip()
            if not eid:
                return error_response("execution_id required", 400, "bad_request",
                                      req.rid)
            # Same pin as /api/chat: capture the chat this approval was
            # made in now, so a chat switch mid-resume can't send the
            # follow-up reply to the wrong conversation.
            cid = site.chat_log.current_id
            token = site.chat_log.begin(cid)
            try:
                reply = site.agent.resume(eid, bool(body.get("allow", True)))
                site.chat_log.add_reply(reply, conversation_id=cid)
            finally:
                site.chat_log.end(token)
            reply = dict(reply)
            reply["conversation_id"] = cid
            return json_response({"ok": True, "data": reply}, rid=req.rid)
        if path == ["api", "dashboard"] and method == "GET":
            return json_response({"ok": True, "data": site.agent.dashboard()},
                                 rid=req.rid)
        if path == ["api", "export"] and method == "GET":
            return json_response({"ok": True, "data": site.agent.export_all()},
                                 rid=req.rid)
        if path == ["api", "import"] and method == "POST":
            result = site.agent.import_all(body.get("data", body))
            return json_response({"ok": True, "data": result}, rid=req.rid)
        if path == ["api", "health"] and method == "GET":
            return json_response({"ok": True, "data": site.health()}, rid=req.rid)
        if path == ["api", "config"] and method == "GET":
            return json_response({"ok": True, "data": site.public_config()},
                                 rid=req.rid)

        # live events
        if path == ["api", "events"] and method == "GET":
            return json_response({"ok": True, "data": site.events().history(
                limit=int_arg(q.get("limit"), 100, name="limit"),
                after_id=int_arg(q.get("after_id"), 0, name="after_id"))},
                rid=req.rid)
        if path == ["api", "events"] and method == "DELETE":
            removed = site.events().clear()
            return json_response({"ok": True, "removed": removed}, rid=req.rid)
        if path == ["api", "events", "stream"] and method == "GET":
            return self._sse(req)
        if path == ["api", "events", "last"] and method == "GET":
            return json_response({"ok": True,
                                  "data": {"last_id": site.events().last_id()}},
                                 rid=req.rid)

        # tools / tasks / memory / experiences
        if path == ["api", "tools"] and method == "GET":
            return json_response({"ok": True,
                                  "data": {"tools": site.registry().list(),
                                           "stats": site.registry().stats()}},
                                 rid=req.rid)
        if path == ["api", "tasks"] and method == "GET":
            return json_response({"ok": True,
                                  "data": site.tasks().list(
                                      status=q.get("status") or None,
                                      type=q.get("type") or None)}, rid=req.rid)
        if path == ["api", "tasks"] and method == "POST":
            goal = (body.get("goal") or "").strip()
            if not goal:
                return error_response("goal required", 400, "bad_request",
                                      req.rid)
            t = site.tasks().create(goal=goal, type=body.get("type", "manual"),
                                    priority=int_arg(body.get("priority"), 0,
                                                     name="priority"),
                                    description=body.get("description", ""))
            return json_response({"ok": True, "data": t}, 201, req.rid)
        if len(path) == 3 and path[0] == "api" and path[1] == "tasks" and method == "GET":
            # generic task engine: per-id lookup (the old plugin-owned
            # per-id PATCH routes were removed with the plugin system)
            t = site.tasks().get(int(path[2])) if path[2].isdigit() else None
            if not t:
                return error_response("task not found", 404, "bad_request",
                                      req.rid)
            return json_response({"ok": True, "data": t}, rid=req.rid)
        if path == ["api", "memory"] and method == "GET":
            return json_response({"ok": True, "data": site.memory().all(
                limit=int_arg(q.get("limit"), 100, name="limit"),
                category=q.get("category") or None)}, rid=req.rid)
        if path == ["api", "memory"] and method == "POST":
            content = (body.get("content") or "").strip()
            if not content:
                return error_response("content required", 400, "bad_request",
                                      req.rid)
            try:
                _imp = float(body.get("importance", 0.5))
            except (TypeError, ValueError):
                _imp = 0.5
            m = site.memory().save(
                content, body.get("category", "note"),
                body.get("tags", ""), source="api",
                layer=body.get("layer", "long"), importance=_imp)
            return json_response({"ok": True, "data": m}, 201, req.rid)
        if path == ["api", "memory", "search"] and method == "GET":
            qq = q.get("query", "")
            try:
                _min_imp = float(q.get("min_importance")) if q.get("min_importance") else None
            except (TypeError, ValueError):
                _min_imp = None
            return json_response({"ok": True, "data": site.memory().search(
                qq, k=int_arg(q.get("k"), 5, name="k"),
                layer=q.get("layer") or None,
                min_importance=_min_imp)}, rid=req.rid)
        if len(path) == 3 and path[0] == "api" and path[1] == "memory" and method == "DELETE":
            site.memory().forget(path[2])
            return json_response({"ok": True}, rid=req.rid)
        if path == ["api", "experiences"] and method == "GET":
            return json_response({"ok": True, "data": site.experiences().stats()},
                                 rid=req.rid)

        # workflows + scheduler
        if path == ["api", "workflows"] and method == "GET":
            return json_response({"ok": True,
                                  "data": site.workflows().list_definitions()},
                                 rid=req.rid)
        if path == ["api", "workflows"] and method == "POST":
            steps = body.get("steps") or []
            if not isinstance(steps, list):
                return error_response("steps must be a list", 400, "bad_request",
                                      req.rid)
            wf = site.workflows().define(body.get("name", ""),
                                         body.get("description", ""), steps)
            return json_response({"ok": True, "data": wf}, 201, req.rid)
        if len(path) == 4 and path[:2] == ["api", "workflows"] and path[3] == "run" and method == "POST":
            run = site.workflows().run(workflow_id=int_arg(
                                           path[2], name="workflow_id"),
                                       params=body.get("params") or {})
            return json_response({"ok": True, "data": run}, rid=req.rid)
        if path == ["api", "workflows", "runs"] and method == "GET":
            return json_response({"ok": True, "data": site.workflows().list_runs()},
                                 rid=req.rid)
        if path == ["api", "schedules"] and method == "GET":
            sched = site.scheduler()
            return json_response({"ok": True,
                                  "data": (sched.list() if sched else [])},
                                 rid=req.rid)
        if path == ["api", "schedules"] and method == "POST":
            sc = site.scheduler()
            if not sc:
                # Same structured "scheduler disabled" the PATCH/DELETE branch
                # returns — a bare 404 (the old fall-through) was misleading.
                return error_response("scheduler disabled", 400, "bad_request",
                                      req.rid)
            s = sc.add(body.get("name", "sched"),
                       body.get("kind", "daily"),
                       body.get("value", ""),
                       int_arg(body.get("workflow_id"), 0,
                               name="workflow_id"),
                       body.get("params") or {})
            return json_response({"ok": True, "data": s}, 201, req.rid)
        if len(path) == 3 and path[:2] == ["api", "schedules"] and method in ("PATCH", "DELETE"):
            sc = site.scheduler()
            if not sc:
                return error_response("scheduler disabled", 400, "bad_request",
                                      req.rid)
            if method == "DELETE":
                sc.delete(int_arg(path[2], name="schedule id"))
                return json_response({"ok": True}, rid=req.rid)
            # PATCH: enable/disable only (schedule fields are fixed)
            enabled = body.get("enabled")
            if enabled is None:
                return error_response("enabled required", 400, "bad_request",
                                      req.rid)
            row = sc.set_enabled(int_arg(path[2], name="schedule id"),
                                 bool(enabled))
            if row:
                return json_response({"ok": True, "data": row}, rid=req.rid)
            return error_response("schedule not found", 404, "bad_request",
                                  req.rid)

        # artifact serving: /api/v1/artifacts/{id}/{filename}
        if (len(path) == 4 and path[:2] == ["api", "artifacts"]
                and method == "GET"):
            return self._serve_artifact(path[2], path[3], req)
        # upload serving: /api/v1/uploads/{filename}
        if (len(path) == 3 and path[:2] == ["api", "uploads"]
                and method == "GET"):
            return self._serve_upload(path[2], req)

        # /api/v1 endpoints (v1 prefix already stripped by resolve_v1)
        v1 = self._handle_v1(req, path)
        if v1 is not None:
            return v1

        # ai providers
        if path == ["api", "providers"] and method == "GET":
            data = site.router().stats()
            # The AI Providers health tab only ever shows providers that
            # can actually be routed to — an API key AND a base URL
            # AND at least one model are all required (section 3 of the
            # module docstring: without all three nothing can be
            # called), so a partially-configured provider is dropped
            # here rather than shown as a confusing "0 models" row.
            provs = data.get("providers") or {}
            data["providers"] = {
                n: p for n, p in provs.items()
                if p.get("credentials") and p.get("base_url") and p.get("models")
            }
            return json_response({"ok": True, "data": data}, rid=req.rid)

        # Astra AI Gateway — separate system, reported outside the
        # provider table (never a provider)
        if path == ["api", "gateway", "health"] and method == "GET":
            r = site.router()
            gw = r.gateway_health() if r is not None else \
                {"state": "not_configured", "connections": []}
            return json_response({"ok": True, "data": gw}, rid=req.rid)

        # orchestrator / agents — the orchestrator was removed; chat now
        # runs through the Gateway pipeline (see astra/ai/chat_pipeline.py).
        # These routes stay so old clients get a clear answer, not a 500.
        if path[:2] in (["api", "agents"], ["api", "executions"]):
            if site.orchestrator() is None:
                if path == ["api", "agents"] and method == "GET":
                    return json_response({"ok": True, "data": {
                        "recent": [], "stats": {}}}, rid=req.rid)
                if path == ["api", "executions"] and method == "GET":
                    return json_response({"ok": True, "data": []}, rid=req.rid)
                return error_response(
                    "orchestrator removed — use POST /api/chat", 410,
                    "bad_request", req.rid)
        if path == ["api", "agents"] and method == "GET":
            return json_response({"ok": True,
                                  "data": {"recent": site.orchestrator().recent(),
                                           "stats": site.orchestrator().stats()}},
                                 rid=req.rid)
        if path == ["api", "agents"] and method == "POST":
            goal = (body.get("goal") or "").strip()
            if not goal:
                return error_response("goal required", 400, "bad_request",
                                      req.rid)
            r = site.orchestrator().submit(goal, sync=bool(body.get("sync", False)))
            return json_response({"ok": True, "data": r}, 201, req.rid)
        if len(path) == 3 and path[0] == "api" and path[1] == "agents" and method == "GET":
            return json_response({"ok": True,
                                  "data": site.orchestrator().state(path[2])},
                                 rid=req.rid)
        if len(path) == 4 and path[0] == "api" and path[1] == "agents" and path[3] == "resume" and method == "POST":
            return json_response({"ok": True, "data": site.orchestrator().resume(
                path[2], bool(body.get("allow", True)))}, rid=req.rid)
        if len(path) == 4 and path[0] == "api" and path[1] == "agents" and path[3] == "cancel" and method == "POST":
            return json_response({"ok": True,
                                  "data": site.orchestrator().cancel(path[2])},
                                 rid=req.rid)
        if path == ["api", "executions"] and method == "GET":
            return json_response({"ok": True,
                                  "data": site.orchestrator().recent()},
                                 rid=req.rid)

        # NOTE: plugin management (GET/enable/disable /api/plugins) and
        # per-plugin route dispatch removed along with the plugin system.
        # plugins/ is an empty placeholder — see plugins/README.md.
        return error_response("unknown route", 404, "bad_request", req.rid)

    # -- /api/v1 endpoints --------------------------------------------------
    def _handle_v1(self, req: Request, path: list):
        """Production endpoints: model registry, router status/stats,
        provider admin, Web3 transactions + policy, metrics. Returns a
        response when the route matched, else None."""
        s, method, body = self.site, req.method, req.body
        if path == ["api", "metrics"] and method == "GET":
            return json_response({"ok": True, "data": s.metrics()}, rid=req.rid)
        if path == ["api", "models"] and method == "GET":
            return self._models(req)
        if path == ["api", "models", "refresh"] and method == "POST":
            disco = s._get("discovery")
            if disco is None:
                return error_response("model discovery unavailable", 400,
                                      "model_unavailable", req.rid)
            disco.refresh_all(force=True)
            return json_response({"ok": True, "data": {"refreshed": True}},
                                 rid=req.rid)
        if path == ["api", "router", "status"] and method == "GET":
            return json_response({"ok": True, "data": s.router().health()},
                                 rid=req.rid)
        if path == ["api", "router", "stats"] and method == "GET":
            r = s.router()
            return json_response({"ok": True,
                                  "data": {"routing": r.routing_stats(),
                                           "task": r.task_stats(),
                                           "last_route": r.last_route()}},
                                 rid=req.rid)
        # provider admin: /api/v1/providers/<name>/refresh|enable|disable|test|reset-health
        # (fixed off-by-one: "api"+"providers"+<name>+<action> is 4 segments,
        # not 5 — the old `len(path) == 5` check meant this route, including
        # refresh/enable/disable, could never actually match a real request)
        if (len(path) == 4 and path[:2] == ["api", "providers"]
                and method == "POST"
                and path[3] in ("refresh", "enable", "disable", "test", "reset-health")):
            return self._provider_admin(req, path[2], path[3])
        # single-model test: /api/v1/providers/<name>/test/<model> — probes
        # just that one (provider, model) pair so the browser gets each
        # model's result the instant it's ready, instead of waiting for
        # every model on the provider to finish (see test_provider_model).
        if (len(path) == 5 and path[:2] == ["api", "providers"]
                and path[3] == "test" and method == "POST"):
            return self._provider_model_test(req, path[2], path[4])
        # one-click "Gateway test": probes every provider AND every
        # Astra AI Gateway connection, saving each result as it completes.
        if path == ["api", "providers", "test-all"] and method == "POST":
            return self._providers_test_all(req)
        if path == ["api", "gateway", "test"] and method == "POST":
            return self._gateway_test(req)
        # single-connection Gateway test: /api/v1/gateway/<connection>/test
        if (len(path) == 4 and path[:2] == ["api", "gateway"]
                and path[3] == "test" and method == "POST"):
            return self._gateway_test_one(req, path[2])
        # single-model Gateway test: /api/v1/gateway/<connection>/test/<model>
        # — probes just that one (connection, model) pair, mirroring the
        # provider per-model endpoint above, so the browser gets each
        # model's result the instant it's ready.
        if (len(path) == 5 and path[:2] == ["api", "gateway"]
                and path[3] == "test" and method == "POST"):
            return self._gateway_model_test(req, path[2], path[4])
        # web3
        if path[:3] == ["api", "web3", "transactions"] and method == "GET":
            return self._web3_tx_list(req, path)
        if path == ["api", "web3", "transaction-policy"] and method == "GET":
            return self._web3_policy(req)
        if (path == ["api", "web3", "transaction-policy", "mode"]
                and method == "POST"):
            return self._web3_policy_mode(req, body)
        # operator approve/reject for a CONFIRM-mode WAITING_USER transaction:
        # /api/v1/web3/transactions/<tx_id>/authorize | reject
        if (len(path) == 5 and path[:3] == ["api", "web3", "transactions"]
                and method == "POST" and path[4] in ("authorize", "reject")):
            return self._web3_tx_action(req, path[3], path[4], body)
        return None

    def _models(self, req: Request) -> Response:
        s = self.site
        reg = s._get("model_registry") or s._get("models")
        if reg is None:
            return error_response("model registry unavailable", 400,
                                  "model_unavailable", req.rid)
        router = s.router()
        health = router.health() if router else {}
        models = []
        for m in reg.all_models():
            row = m.to_dict()
            row["status"] = health.get(m.provider, {}).get("status", "unknown")
            models.append(row)
        summary = {"count": reg.count(),
                   "by_provider": {p: len(reg.for_provider(p))
                                   for p in reg.providers()},
                   "by_status": reg.status_summary()}
        return json_response({"ok": True,
                              "data": {"models": models,
                                       "summary": summary}}, rid=req.rid)

    def _provider_admin(self, req: Request, name, action) -> Response:
        s = self.site
        reg = s._get("provider_registry")
        router = s.router()
        if router is None or reg is None:
            return error_response("providers unavailable", 400,
                                  "provider_unavailable", req.rid)
        if action == "refresh":
            disco = s._get("discovery")
            if disco is not None:
                disco.discover(name, force=True)
            router.reset_health(name)
            return json_response({"ok": True,
                                  "data": {"provider": name,
                                           "refreshed": True}}, rid=req.rid)
        if action in ("enable", "disable"):
            want = action == "enable"
            if want:
                router.enable(name)
            else:
                router.disable(name)
            return json_response({"ok": True,
                                  "data": {"provider": name,
                                           "enabled": want}}, rid=req.rid)
        if action == "test":
            # manual "AI Providers health" per-provider Test button: a
            # real call against this one provider, no fallback to another
            # provider, result saved (router._record_route /
            # _mark_down) the instant this call returns.
            result = router.test_provider(name)
            return json_response({"ok": True, "data": result}, rid=req.rid)
        if action == "reset-health":
            # Called by the UI right before it starts firing per-model test
            # requests at ONE provider: wipes that provider's previously
            # saved calls/errors/latency so this test's numbers start clean
            # instead of adding onto whatever earlier tests had saved — and
            # touches only this provider, every other provider's saved
            # health is untouched.
            router.reset_health(name)
            info = router.health().get(name, {})
            return json_response({"ok": True, "data": {
                "provider": name,
                "calls": info.get("calls", 0),
                "errors": info.get("errors", 0),
                "state": info.get("state"),
                "healthy": info.get("healthy"),
            }}, rid=req.rid)
        return error_response(f"unknown action: {action}", 400, "bad_request",
                              req.rid)

    def _provider_model_test(self, req: Request, name, model_id) -> Response:
        """Probe exactly one (provider, model) pair. Backs the per-model UI
        test calls so the browser can fire one request per model and update
        each row the moment that model's own response comes back, instead
        of waiting on the whole provider's model list."""
        router = self.site.router()
        if router is None:
            return error_response("providers unavailable", 400,
                                  "provider_unavailable", req.rid)
        # optional ?key=<key_id>: force this test through one specific API key
        # (ids come from /api/providers -> providers.<n>.keys[].key_id)
        key_id = (req.query.get("key") or "").strip() or None
        result = router.test_provider_model(name, model_id, key_id)
        return json_response({"ok": True, "data": result}, rid=req.rid)

    def _providers_test_all(self, req: Request) -> Response:
        """One-click 'Gateway test': probe every provider AND every Astra AI
        Gateway connection. Each provider's saved health (calls/errors/
        latency) is wiped right before this run — via reset_all_health() —
        so the numbers this produces reflect only THIS run, not history
        piled up from every earlier test; that reset covers every provider
        together since this is the "test all" entry point. Each probe's
        result is then persisted the moment that probe finishes (see
        AstraRouter.test_provider / AstraAIGateway.test_connection) — this
        loop doesn't wait for every test to *succeed*, only for each to
        *finish* before moving on, so a slow/dead provider never blocks the
        others from being recorded."""
        router = self.site.router()
        if router is None:
            return error_response("providers unavailable", 400,
                                  "provider_unavailable", req.rid)
        router.reset_all_health()
        providers = router.test_all_providers()
        gw = getattr(router, "gateway", None)
        connections = gw.test_all_connections() if gw is not None else []
        return json_response({"ok": True, "data": {
            "providers": providers, "gateway_connections": connections}},
            rid=req.rid)

    def _gateway_test(self, req: Request) -> Response:
        """Test only the Astra AI Gateway's four connections."""
        router = self.site.router()
        gw = getattr(router, "gateway", None) if router else None
        if gw is None:
            return error_response("Astra AI Gateway not configured", 400,
                                  "gateway_unavailable", req.rid)
        return json_response({"ok": True,
                              "data": {"connections": gw.test_all_connections()}},
                             rid=req.rid)

    def _gateway_test_one(self, req: Request, name) -> Response:
        """Test one Astra AI Gateway connection (e.g. 'astra-gw-gemini') —
        every model it exposes, not just one."""
        router = self.site.router()
        gw = getattr(router, "gateway", None) if router else None
        if gw is None:
            return error_response("Astra AI Gateway not configured", 400,
                                  "gateway_unavailable", req.rid)
        return json_response({"ok": True,
                              "data": gw.test_connection_by_name(name)},
                             rid=req.rid)

    def _gateway_model_test(self, req: Request, name, model_id) -> Response:
        """Probe exactly one (connection, model) pair. Backs the per-model
        UI test calls so the browser can fire one request per model and
        update each row the moment that model's own response comes back —
        same streaming behaviour the provider cards use."""
        router = self.site.router()
        gw = getattr(router, "gateway", None) if router else None
        if gw is None:
            return error_response("Astra AI Gateway not configured", 400,
                                  "gateway_unavailable", req.rid)
        result = gw.test_connection_model_by_name(name, model_id)
        return json_response({"ok": True, "data": result}, rid=req.rid)

    def _web3_tx_list(self, req: Request, path):
        s = self.site
        tx = s._get("tx_manager")
        if tx is None:
            return error_response("Web3 transaction manager unavailable", 400,
                                  "web3_unavailable", req.rid)
        if len(path) == 3:
            return json_response({"ok": True,
                                  "data": {"transactions": tx.list(100),
                                           "stats": tx.stats()}}, rid=req.rid)
        if len(path) == 4:
            row = tx.get(path[3])
            if row is None or row.get("status") == "UNKNOWN":
                return error_response("transaction not found", 404,
                                      "transaction_not_found", req.rid)
            return json_response({"ok": True, "data": row}, rid=req.rid)
        return None

    def _web3_policy(self, req: Request) -> Response:
        s = self.site
        policy = s._get("web3_policy") or s._get("tx_policy")
        if policy is None:
            return error_response("transaction policy unavailable", 400,
                                  "web3_unavailable", req.rid)
        data = {"policy": policy.describe(),
                "mode": policy.mode,
                "stopped": bool(getattr(s._get("tx_manager"), "stopped", False))}
        return json_response({"ok": True, "data": data}, rid=req.rid)

    def _web3_policy_mode(self, req: Request, body) -> Response:
        s = self.site
        # operator-only: LLM must never change web3 mode; require a token
        if not s.operator_token:
            return error_response(
                "set ASTRA_TOKEN to allow Web3 mode changes", 403,
                "authorization", req.rid)
        policy = s._get("web3_policy") or s._get("tx_policy")
        tx = s._get("tx_manager")
        if policy is None or tx is None:
            return error_response("transaction policy unavailable", 400,
                                  "web3_unavailable", req.rid)
        mode = (body.get("mode") or "").strip().upper()
        if mode not in ("CONFIRM", "AUTO"):
            return error_response("mode must be CONFIRM or AUTO", 400,
                                  "validation", req.rid)
        policy.set_mode(mode)
        return json_response({"ok": True, "data": {"mode": policy.mode}},
                             rid=req.rid)

    def _web3_tx_action(self, req: Request, tx_id: str, action: str,
                        body) -> Response:
        """Operator-only approve/reject for a pending (CONFIRM-mode)
        transaction. The LLM tool surface never reaches authorize()/
        sign_and_broadcast()/reject() directly — only this gated endpoint
        does, and only when the operator has explicitly set ASTRA_TOKEN.
        Switching the global mode to AUTO does not touch existing
        WAITING_USER/PREPARED transactions; they still need this call.
        """
        s = self.site
        if not s.operator_token:
            return error_response(
                "set ASTRA_TOKEN to allow Web3 transaction approval", 403,
                "authorization", req.rid)
        tx = s._get("tx_manager")
        if tx is None:
            return error_response("Web3 transaction manager unavailable", 400,
                                  "web3_unavailable", req.rid)
        from .web3.policy import (TransactionPolicyError,
                                  TransactionRejectedError,
                                  TransactionFailedError)
        try:
            if action == "reject":
                reason = ((body or {}).get("reason")
                          if isinstance(body, dict) else None) or \
                    "operator rejected"
                rec = tx.reject(tx_id, reason=reason)
            else:
                tx.authorize(tx_id)
                rec = tx.sign_and_broadcast(tx_id)
            return json_response({"ok": True, "data": rec}, rid=req.rid)
        except (TransactionPolicyError, TransactionRejectedError,
                TransactionFailedError) as exc:
            return error_response(str(exc), 400, "transaction_policy", req.rid)


# ── Server-Sent Events ───────────────────────────────────────────────────────

def _sse_start_id(site: AstraSite, req: Request) -> int:
    """Where a feed should resume from.

    Last-Event-ID (sent automatically by EventSource on its own reconnects)
    takes priority so a dropped/45s-recycled connection resumes exactly where
    it left off instead of skipping or replaying events. ?after_id= is what
    the frontend passes on the very first connect, right after it has loaded
    history via GET /api/events, so nothing between "history" and "live" is
    missed. With neither, default to "now" (last_id())."""
    def _int_or_last(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return site.events().last_id()

    after_header = req.headers.get("Last-Event-ID")
    after_q = req.query.get("after_id")
    if after_header:
        return _int_or_last(after_header)
    if after_q:
        return _int_or_last(after_q)
    return site.events().last_id()


def sse_frame_bytes(event: dict) -> bytes:
    """One SSE frame. Redacted like every other outbound payload, so a leaky
    subsystem cannot egress a secret through the live feed either."""
    payload = redact({"id": event["id"], "kind": event["kind"],
                      "agent": event.get("agent", ""),
                      "data": event.get("data", {}),
                      "created_at": event.get("created_at", "")})
    return (f"id: {event['id']}\ndata: " +
            json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


SSE_POLL_S = 0.7      # idle poll interval
SSE_DEADLINE_S = 45   # how long one connection stays open before recycling
SSE_MAX_FRAMES = 200  # cap per connection so a burst cannot monopolise it


async def sse_frames(site: AstraSite, req: Request):
    """The SSE live feed, one frame at a time.

    Async on purpose: Starlette drives a *sync* generator inside its shared
    anyio worker threadpool, and an idle Live tab holds its connection open for
    up to 45s — a handful of them would occupy the very threads that run agent
    turns (40 by default, and every blocked turn is a hung request). Here the
    wait is `asyncio.sleep` and only the short SQLite read is handed to the
    default executor, so an idle feed costs no worker thread at all.
    """
    loop = asyncio.get_running_loop()
    after = _sse_start_id(site, req)
    written, deadline = 0, time.time() + SSE_DEADLINE_S
    while time.time() < deadline:
        # A short, bounded query: run it off the loop, but hold nothing while
        # waiting for the next poll.
        evs = await loop.run_in_executor(None, site.events().since, after)
        if evs:
            for e in evs:
                yield sse_frame_bytes(e)
                after = e["id"]
                written += 1
            if written >= SSE_MAX_FRAMES:
                break
        else:
            await asyncio.sleep(SSE_POLL_S)
