"""Astra web server: serves the plugin-driven UI and a shared JSON API.

Zero dependencies — stdlib http.server. Threaded so plugins that do slow
work (network research) don't block the UI. Routes come from each plugin's
`routes()`; a small matcher turns `("PATCH", ("api","wallets","<id>"), h)`
into a call with `params={"id": <int>}`.

Security hardening (production build):
  * optional operator auth — set ASTRA_TOKEN: all /api/* then require a
    Bearer token / X-Astra-Token header / ?token= (SSE has no headers);
    unset token = open (safe local-first default)
  * per-IP rate limiting — ASTRA_API_RATE_LIMIT (default 300 requests/min)
  * request body cap — ASTRA_MAX_BODY_MB (default 10 MB)
  * security headers + Content-Security-Policy on every response
  * X-Request-Id on every response; request_id in every JSON body
  * structured JSON errors {ok, error, error_code, request_id} — never
    leaks stack traces (message detail gated to non-production)
  * every response redacted through security.redact — no secrets egress

Versioning: every /api/... route is ALSO served under /api/v1/... (the v1
segment is stripped before dispatch, so there is no duplicated code path).
New endpoints exist only under /api/v1.

Core (non-plugin) endpoints (all old routes stay backward-compatible):

  GET  /api/manifest        agent name + plugin tabs + core tabs
  POST /api/chat            {message} -> agent reply {reply, action, data, ok}
  GET  /api/dashboard       aggregated plugin summary() blocks
  GET  /api/export          aggregated plugin export()
  POST /api/import          aggregate import across plugins

System endpoints:

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
  GET  /api/gateway/health  Astra AI Gateway status (4 connections + fallback)
  GET  /api/agents          recent executions; GET {exec}/… state
  POST /api/agents          submit a goal to the orchestrator
  POST /api/agents/{exec}/resume | /cancel   control WAITING_USER runs
  GET  /api/executions      alias for /api/agents
  GET  /api/plugins         list plugins (+enabled); POST {slug}/enable|disable

New /api/v1 endpoints:

  GET  /api/v1/models            model registry (capabilities + health)
  POST /api/v1/models/refresh    re-run provider model discovery
  GET  /api/v1/router/status     routing health
  GET  /api/v1/router/stats      routing + task statistics
  POST /api/v1/providers/<name>/refresh | enable | disable
  GET  /api/v1/web3/transactions              (+ /{tx_id})
  GET  /api/v1/web3/transaction-policy        (mode/limits/whitelist/stop)
  POST /api/v1/web3/transaction-policy/mode   (operator token required)
  POST /api/v1/web3/transactions/{tx_id}/authorize  (CONFIRM-mode approve; operator token required)
  POST /api/v1/web3/transactions/{tx_id}/reject     (CONFIRM-mode reject; operator token required)
  GET  /api/metrics              server + subsystem metrics
"""
from __future__ import annotations

import hmac
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .agent import Agent
from .core import Plugin
from .security import (ApiError, RateLimiter, make_request_id, redact,
                       redact_text)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")

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
    {"tab": "dashboard", "label": "📊 Dashboard", "core": True},
    {"tab": "live", "label": "⚡ Live", "core": True},
    {"tab": "assistant", "label": "🤖 Assistant", "core": True},
    {"tab": "providers", "label": "🔌 Providers", "core": True},
    {"tab": "router", "label": "🧠 Router", "core": True},
    {"tab": "web3", "label": "⛓️ Wallet", "core": True},
    {"tab": "backup", "label": "💾 Backup", "core": True},
]

# Appended after every plugin tab (Airdrops, etc.) — see manifest().
LOGS_TAB = {"tab": "logs", "label": "📡 Activity Log", "core": True}

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


def _json(handler: "AstraHandler", payload, code: int = 200) -> None:
    """Serialise a payload as JSON with full hardening. The payload is
    redacted defensively so a subsystem that leaks a secret can never egress
    it through the API. The request_id rides both header and body."""
    rid = getattr(handler, "_rid", "")
    if isinstance(payload, dict) and "request_id" not in payload:
        payload = {**payload, "request_id": rid}
    body = json.dumps(redact(payload), ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("X-Request-Id", rid)
    handler.apply_security_headers()
    handler.apply_cors_headers()
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _json_ok(handler: "AstraHandler", payload, code: int = 200) -> None:
    _json(handler, payload, code)


def _json_err(handler: "AstraHandler", message: str, code: int = 400,
              error_code: str = "bad_request") -> None:
    """Structured error envelope — no stack traces, ever."""
    _json(handler, {"ok": False, "error": message,
                    "error_code": error_code,
                    "request_id": getattr(handler, "_rid", "")}, code)


def _parse_multipart_body(raw: bytes, content_type: str) -> tuple[dict, list]:
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
        """Read a JSON body with a hard size cap (prevents memory blowup)."""
        max_bytes = self.server.max_body_bytes
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > max_bytes:
            self._drain(min(length, 64 * 1024 * 1024))
            raise ApiError("payload_too_large",
                           f"request body exceeds {max_bytes} bytes limit", 413)
        try:
            raw = self.rfile.read(length)
        except Exception:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _is_multipart(self) -> bool:
        ct = self.headers.get("Content-Type", "")
        return "multipart/form-data" in ct

    def _read_multipart(self) -> tuple[dict, list]:
        """Parse multipart/form-data. Returns (fields_dict, files_list).
        Each file in files_list is {filename, data (bytes), content_type}.

        Implemented by hand (no `cgi` module — removed in Python 3.13)."""
        max_bytes = self.server.max_body_bytes
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > max_bytes:
            self._drain(min(length, 64 * 1024 * 1024))
            raise ApiError("payload_too_large",
                           f"request body exceeds {max_bytes} bytes limit", 413)
        ct = self.headers.get("Content-Type", "")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            return _parse_multipart_body(raw, ct)
        except Exception:
            return {}, []

    # -- security helpers ------------------------------------------------
    def _drain(self, length: int) -> None:
        """Read and discard up to `length` bytes of an oversized body so the
        client can complete its send and read the error response."""
        remaining = length
        while remaining > 0:
            try:
                chunk = self.rfile.read(min(64 * 1024, remaining))
            except Exception:
                return
            if not chunk:
                return
            remaining -= len(chunk)

    def apply_security_headers(self) -> None:
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)

    def apply_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        allowed = self.server.cors_origin(origin)
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")
            if self.command == "OPTIONS":
                self.send_header("Access-Control-Allow-Methods",
                                 "GET,POST,PATCH,DELETE,OPTIONS")
                self.send_header("Access-Control-Allow-Headers",
                                 "Content-Type, X-Astra-Token, Authorization")
                self.send_header("Access-Control-Max-Age", "600")

    def _authorized(self) -> bool:
        """Operator token gate. ASTRA_TOKEN unset = open (local-first);
        set = Bearer / X-Astra-Token / ?token= must match, constant-time."""
        token = self.server.operator_token
        if not token:
            return True
        supplied = self.headers.get("Authorization", "")
        if supplied.lower().startswith("bearer "):
            candidate = supplied[7:].strip()
        else:
            candidate = (self.headers.get("X-Astra-Token", "")
                         or self._query().get("token", ""))
        return bool(candidate) and hmac.compare_digest(candidate, token)

    def _rate_limited(self) -> bool:
        limiter = self.server.rate_limiter
        if not limiter:
            return False
        ip = (self.client_address or ("unknown", 0))[0]
        return not limiter.allow(ip)

    def _resolve_v1(self, path: list[str]) -> list[str]:
        """/api/v1/<rest> -> /api/<rest> so every legacy route is served
        under the versioned prefix with no duplicated code path."""
        if len(path) >= 3 and path[0] == "api" and path[1] == "v1":
            return ["api"] + path[2:]
        return path

    def _gate(self, path: list[str]):
        """Auth + rate-limit for API traffic. Returns an already-sent marker
        string (truthy) on rejection or None to proceed."""
        if not (path and path[0] == "api"):
            return None
        if not self._authorized():
            _json_err(self, "authentication required", 401,
                      error_code="authentication")
            return "auth"
        if self._rate_limited():
            _json_err(self, "rate limit exceeded", 429, error_code="rate_limit")
            return "rate"
        return None

    def _send_static(self, rel: str) -> None:
        if rel in ("", "/"):
            rel = "index.html"
        rel = rel.lstrip("/")
        if ".." in rel or "\\" in rel:
            _json_err(self, "bad path", 400)
            return
        root = os.path.abspath(STATIC_DIR)
        path = os.path.abspath(os.path.join(root, rel))
        # path-traversal guard: resolved path must stay inside static root
        if not (path == root or path.startswith(root + os.sep)):
            _json_err(self, "forbidden", 403)
            return
        if not os.path.isfile(path):
            self.send_error(404, "not found")
            return
        ctype = CONTENT_TYPES.get(path.rsplit(".", 1)[-1], "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", getattr(self, "_rid", ""))
        self.apply_security_headers()
        self.end_headers()
        self.wfile.write(body)

    # -- multimodal ----------------------------------------------------------
    def _process_uploads(self, files: list) -> list[dict]:
        """Process uploaded files into attachment dicts for the agent."""
        if not files:
            return []
        upload_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        attachments = []
        for f in files[:10]:
            try:
                from astra.core.attachments import process_upload
                att = process_upload(f["data"], f["filename"], upload_dir)
                attachments.append(att.to_dict())
            except Exception as e:
                attachments.append({
                    "filename": f.get("filename", "unknown"),
                    "error": str(e),
                    "processed": False,
                })
        return attachments

    def _serve_upload(self, filename: str) -> None:
        """Serve an uploaded file for preview."""
        upload_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "uploads")
        path = os.path.abspath(os.path.join(upload_dir, filename))
        root = os.path.abspath(upload_dir)
        if not path.startswith(root + os.sep):
            _json_err(self, "forbidden", 403)
            return
        if not os.path.isfile(path):
            _json_err(self, "file not found", 404)
            return
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", getattr(self, "_rid", ""))
        self.apply_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _serve_artifact(self, artifact_id: str, filename: str) -> None:
        """Serve a stored artifact file."""
        artifact_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "artifacts")
        path = os.path.abspath(os.path.join(artifact_dir, artifact_id, filename))
        root = os.path.abspath(artifact_dir)
        if not path.startswith(root + os.sep):
            _json_err(self, "forbidden", 403)
            return
        if not os.path.isfile(path):
            _json_err(self, "artifact not found", 404)
            return
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         f'inline; filename="{filename}"')
        self.send_header("X-Request-Id", getattr(self, "_rid", ""))
        self.apply_security_headers()
        self.end_headers()
        self.wfile.write(body)

    # -- dispatch ------------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        self._rid = make_request_id()
        server, path = self.server, self._resolve_v1(self._path_parts())
        gated = self._gate(path)
        if gated:
            return
        server.note_request(method, path)
        try:
            q = self._query()
            body = {} if self._is_multipart() else self._read_json()
            if not path:
                return self._send_static("index.html")
            if path[0] == "static":
                return self._send_static("/".join(path[1:]))
            if path[0] == "favicon.ico":
                self.send_response(204)
                self.send_header("X-Request-Id", self._rid)
                return self.end_headers()

            # system / manifest endpoints
            if path == ["api", "manifest"]:
                return _json_ok(self, {"ok": True, "data": server.manifest()})
            if path == ["api", "chat"] and method == "POST":
                if self._is_multipart():
                    fields, files = self._read_multipart()
                    msg = fields.get("message", "")
                    ctx = fields.get("context", "")
                    attachments = self._process_uploads(files)
                    reply = server.agent.handle(
                        msg, context=ctx, attachments=attachments or None)
                else:
                    reply = server.agent.handle(
                        body.get("message", ""),
                        context=body.get("context", "") or "")
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
                try:
                    _imp = float(body.get("importance", 0.5))
                except (TypeError, ValueError):
                    _imp = 0.5
                m = server.memory().save(
                    content, body.get("category", "note"),
                    body.get("tags", ""), source="api",
                    layer=body.get("layer", "long"), importance=_imp)
                return _json_ok(self, {"ok": True, "data": m}, 201)
            if path == ["api", "memory", "search"] and method == "GET":
                qq = q.get("query", "")
                try:
                    _min_imp = float(q.get("min_importance")) if q.get("min_importance") else None
                except (TypeError, ValueError):
                    _min_imp = None
                return _json_ok(self, {"ok": True,
                                       "data": server.memory().search(
                                           qq, k=int(q.get("k", 5)),
                                           layer=q.get("layer") or None,
                                           min_importance=_min_imp)})
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

            # artifact serving: /api/v1/artifacts/{id}/{filename}
            if (len(path) == 4 and path[:2] == ["api", "artifacts"]
                    and method == "GET"):
                return self._serve_artifact(path[2], path[3])
            # upload serving: /api/v1/uploads/{filename}
            if (len(path) == 3 and path[:2] == ["api", "uploads"]
                    and method == "GET"):
                return self._serve_upload(path[2])

            # /api/v1 endpoints (v1 prefix already stripped by _resolve_v1)
            if self._handle_v1(path, method, q, body):
                return

            # ai providers
            if path == ["api", "providers"] and method == "GET":
                return _json_ok(self, {"ok": True,
                                       "data": server.router().stats()})

            # Astra AI Gateway — separate system, reported outside the
            # provider table (never a provider)
            if path == ["api", "gateway", "health"] and method == "GET":
                r = server.router()
                gw = r.gateway_health() if r is not None else \
                    {"state": "not_configured", "connections": []}
                return _json_ok(self, {"ok": True, "data": gw})

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
        except ApiError as e:
            server.note_request(method, path, error=True)
            if not e.request_id:
                e.request_id = self._rid
            return _json(self, e.to_dict(), e.status)
        except Exception as e:
            server.note_request(method, path, error=True)
            detail = str(e) if self.server.env != "production" else ""
            msg = f"internal error{': ' + detail if detail else ''}"
            return _json_err(self, msg, 500, error_code="internal")

    # -- /api/v1 endpoints --------------------------------------------------
    def _handle_v1(self, path, method, q, body) -> bool:
        """Production endpoints: model registry, router status/stats,
        provider admin, Web3 transactions + policy, metrics. Returns True
        when the route was handled (response already sent)."""
        s = self.server
        if path == ["api", "metrics"] and method == "GET":
            return self._json_ok_rid({"ok": True, "data": s.metrics()})
        if path == ["api", "models"] and method == "GET":
            return self._models(s)
        if path == ["api", "models", "refresh"] and method == "POST":
            disco = s._get("discovery")
            if disco is None:
                return self._err_rid("model discovery unavailable", 400,
                                     "model_unavailable")
            disco.refresh_all(force=True)
            return self._json_ok_rid({"ok": True, "data": {"refreshed": True}})
        if path == ["api", "router", "status"] and method == "GET":
            return self._json_ok_rid({"ok": True,
                                      "data": s.router().health()})
        if path == ["api", "router", "stats"] and method == "GET":
            r = s.router()
            return self._json_ok_rid({"ok": True,
                                      "data": {"routing": r.routing_stats(),
                                               "task": r.task_stats(),
                                               "last_route": r.last_route()}})
        # provider admin: /api/v1/providers/<name>/refresh|enable|disable
        if (len(path) == 5 and path[:2] == ["api", "providers"]
                and method == "POST"
                and path[4] in ("refresh", "enable", "disable")):
            return self._provider_admin(s, path[2], path[4])
        # web3
        if path[:3] == ["api", "web3", "transactions"] and method == "GET":
            return self._web3_tx_list(s, path)
        if path == ["api", "web3", "transaction-policy"] and method == "GET":
            return self._web3_policy(s)
        if (path == ["api", "web3", "transaction-policy", "mode"]
                and method == "POST"):
            return self._web3_policy_mode(s, body)
        # operator approve/reject for a CONFIRM-mode WAITING_USER transaction:
        # /api/v1/web3/transactions/<tx_id>/authorize | reject
        if (len(path) == 5 and path[:3] == ["api", "web3", "transactions"]
                and method == "POST" and path[4] in ("authorize", "reject")):
            return self._web3_tx_action(s, path[3], path[4], body)
        return False

    def _json_ok_rid(self, payload):
        _json_ok(self, payload)
        return True

    def _err_rid(self, message, code=400, error_code="bad_request"):
        _json_err(self, message, code, error_code)
        return True

    def _models(self, s) -> bool:
        reg = s._get("model_registry") or s._get("models")
        if reg is None:
            return self._err_rid("model registry unavailable", 400,
                                 "model_unavailable")
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
        return self._json_ok_rid({"ok": True,
                                  "data": {"models": models,
                                           "summary": summary}})

    def _provider_admin(self, s, name, action) -> bool:
        reg = s._get("provider_registry")
        router = s.router()
        if router is None or reg is None:
            return self._err_rid("providers unavailable", 400,
                                 "provider_unavailable")
        if action == "refresh":
            disco = s._get("discovery")
            if disco is not None:
                disco.discover(name, force=True)
            router.reset_health(name)
            return self._json_ok_rid({"ok": True,
                                      "data": {"provider": name,
                                               "refreshed": True}})
        if action in ("enable", "disable"):
            want = action == "enable"
            if want:
                router.enable(name)
            else:
                router.disable(name)
            return self._json_ok_rid({"ok": True,
                                      "data": {"provider": name,
                                               "enabled": want}})
        return self._err_rid(f"unknown action: {action}", 400)

    def _web3_tx_list(self, s, path) -> bool:
        tx = s._get("tx_manager")
        if tx is None:
            return self._err_rid("Web3 transaction manager unavailable",
                                 400, "web3_unavailable")
        if len(path) == 3:
            return self._json_ok_rid({"ok": True,
                                      "data": {"transactions": tx.list(100),
                                               "stats": tx.stats()}})
        if len(path) == 4:
            row = tx.get(path[3])
            if row is None or row.get("status") == "UNKNOWN":
                return self._err_rid("transaction not found", 404,
                                     "transaction_not_found")
            return self._json_ok_rid({"ok": True, "data": row})
        return False

    def _web3_policy(self, s) -> bool:
        policy = s._get("web3_policy") or s._get("tx_policy")
        if policy is None:
            return self._err_rid("transaction policy unavailable", 400,
                                 "web3_unavailable")
        data = {"policy": policy.describe(),
                "mode": policy.mode,
                "stopped": bool(getattr(s._get("tx_manager"), "stopped", False))}
        return self._json_ok_rid({"ok": True, "data": data})

    def _web3_policy_mode(self, s, body) -> bool:
        # operator-only: LLM must never change web3 mode; require a token
        if not s.operator_token:
            return self._err_rid(
                "set ASTRA_TOKEN to allow Web3 mode changes", 403,
                "authorization")
        policy = s._get("web3_policy") or s._get("tx_policy")
        tx = s._get("tx_manager")
        if policy is None or tx is None:
            return self._err_rid("transaction policy unavailable", 400,
                                 "web3_unavailable")
        mode = (body.get("mode") or "").strip().upper()
        if mode not in ("CONFIRM", "AUTO"):
            return self._err_rid("mode must be CONFIRM or AUTO", 400,
                                 "validation")
        policy.set_mode(mode)
        return self._json_ok_rid({"ok": True,
                                  "data": {"mode": policy.mode}})

    def _web3_tx_action(self, s, tx_id: str, action: str, body) -> bool:
        """Operator-only approve/reject for a pending (CONFIRM-mode)
        transaction. The LLM tool surface never reaches authorize()/
        sign_and_broadcast()/reject() directly — only this gated endpoint
        does, and only when the operator has explicitly set ASTRA_TOKEN.
        Switching the global mode to AUTO does not touch existing
        WAITING_USER/PREPARED transactions; they still need this call.
        """
        if not s.operator_token:
            return self._err_rid(
                "set ASTRA_TOKEN to allow Web3 transaction approval", 403,
                "authorization")
        tx = s._get("tx_manager")
        if tx is None:
            return self._err_rid("Web3 transaction manager unavailable", 400,
                                 "web3_unavailable")
        from astra.web3.policy import (TransactionPolicyError,
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
            return self._json_ok_rid({"ok": True, "data": rec})
        except (TransactionPolicyError, TransactionRejectedError,
                TransactionFailedError) as exc:
            return self._err_rid(str(exc), 400, "transaction_policy")

    # -- SSE ------------------------------------------------------------------
    def _sse(self):
        """Server-Sent Events feed. Tails new events (poll inside the request
        thread); writes `data:` frames and flushes, so the browser Live tab
        streams activity with no WebSocket dependency."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Request-Id", getattr(self, "_rid", ""))
        self.apply_security_headers()
        self.apply_cors_headers()
        self.end_headers()
        after = int(self._query().get("after_id") or self.server.events().last_id())
        written, idle, deadline = 0, 0, time.time() + 45
        try:
            while time.time() < deadline:
                evs = self.server.events().since(after)
                if evs:
                    for e in evs:
                        frame = (f"id: {e['id']}\ndata: " +
                                 json.dumps(redact({"id": e["id"], "kind": e["kind"],
                                             "agent": e.get("agent", ""),
                                             "data": e.get("data", {}),
                                             "created_at": e.get("created_at", "")}),
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
        self.send_header("X-Request-Id", getattr(self, "_rid", ""))
        self.apply_security_headers()
        self.apply_cors_headers()
        self.end_headers()


class AstraServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, store, agent: Agent, plugins: list[Plugin], stack=None):
        super().__init__(addr, AstraHandler)
        self.store = store
        self.agent = agent
        self.plugins = plugins
        self._stack = stack or {}
        cfg = self._get("config")
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
        try:
            self.bind_host = addr[0]
        except Exception:
            self.bind_host = "0.0.0.0"
        # -- observability: tiny in-memory request counters --------------------
        self._req_lock = __import__("threading").Lock()
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
        # unconfigured providers report "not configured", not unhealthy — a
        # missing API key must not flip /api/health red
        provider_unhealthy = any(
            p.get("healthy") is False
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
        tabs.append(dict(LOGS_TAB))
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