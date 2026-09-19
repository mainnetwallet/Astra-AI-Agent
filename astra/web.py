"""Astra web server — stdlib adapter over `astra.web_core`.

This module owns the socket: it parses one request off the wire, hands
the neutral `web_core.Request` to the shared router, and writes the
`web_core.Response` back. It decides nothing about routing, auth or
response shape — see astra/web_core.py. The optional FastAPI server
(astra/web_fastapi.py) calls the very same router, so the two cannot
disagree about behaviour.

Zero dependencies — stdlib http.server. Threaded, so a slow tool call
(network research, a provider probe) never blocks the UI. The route table
itself lives in astra/web_core.py; `route_matcher` there is the helper that
maps a pattern like `("PATCH", ("api","wallets","<id>"))` onto a request
path with `params={"id": <int>}` (kept for the plugin route contract).

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

Core (non-plugin) endpoints (all old routes stay backward-compatible):

  GET  /api/manifest        agent name + plugin tabs + core tabs
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
  POST /api/chat/resume     {execution_id, allow} -> approve/reject a pending
                             WAITING_USER tool call inline from chat (same
                             reply shape as /api/chat; no separate tab needed)
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

import json

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .security import (ApiError, RateLimiter, make_request_id, redact,
                       redact_text)  # noqa: F401  (re-exported for callers)
from .web_core import (AGENT_NAME, CONTENT_TYPES, CORE_TABS, LOGS_TAB,
                       SECURITY_HEADERS, STATIC_DIR,  # noqa: F401
                       AstraSite, Headers, Request, Response, WebApp,
                       api_error_response, error_response, json_response,
                       parse_multipart_body, parse_query, response_headers,
                       split_path)

__all__ = ["AGENT_NAME", "AstraHandler", "AstraServer", "AstraSite",
           "WebApp", "STATIC_DIR", "CONTENT_TYPES", "CORE_TABS",
           "LOGS_TAB", "SECURITY_HEADERS", "Request", "Response", "Headers"]


def _json(handler: "AstraHandler", payload, code: int = 200) -> None:
    """Serialise a payload as JSON with full hardening (compat helper)."""
    handler._emit(json_response(payload, code, getattr(handler, "_rid", "")),
                  handler._last_request(), handler.command)


def _json_ok(handler: "AstraHandler", payload, code: int = 200) -> None:
    _json(handler, payload, code)


def _json_err(handler: "AstraHandler", message: str, code: int = 400,
              error_code: str = "bad_request") -> None:
    """Structured error envelope — no stack traces, ever."""
    handler._emit(error_response(message, code, error_code,
                                 getattr(handler, "_rid", "")),
                  handler._last_request(), handler.command)


class AstraHandler(BaseHTTPRequestHandler):
    server: "AstraServer"

    def log_message(self, fmt, *args):  # silence request noise
        pass

    # -- plumbing ------------------------------------------------------------
    def _path_parts(self) -> list:
        # Split on the *raw* "/" first, then percent-decode each segment, so an
        # encoded model id like "openai%2Fgpt-oss-120b" or "%40cf%2Fmeta%2F..."
        # arrives as one segment with its real "/" and "@" restored.
        return split_path(self.path)

    def _query(self) -> dict:
        return parse_query(self.path)

    def _content_length(self) -> int:
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return 0

    def _is_multipart(self) -> bool:
        return "multipart/form-data" in (self.headers.get("Content-Type") or "")

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

    def _read_json(self) -> dict:
        """Read a JSON body with a hard size cap (prevents memory blowup)."""
        max_bytes = self.server.max_body_bytes
        length = self._content_length()
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

    def _read_multipart(self) -> tuple:
        """Parse multipart/form-data. Returns (fields_dict, files_list).
        Each file in files_list is {filename, data (bytes), content_type}.

        Implemented by hand (no `cgi` module — removed in Python 3.13)."""
        max_bytes = self.server.max_body_bytes
        length = self._content_length()
        if length > max_bytes:
            self._drain(min(length, 64 * 1024 * 1024))
            raise ApiError("payload_too_large",
                           f"request body exceeds {max_bytes} bytes limit", 413)
        ct = self.headers.get("Content-Type", "")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            return parse_multipart_body(raw, ct)
        except Exception:
            return {}, []

    def apply_security_headers(self) -> None:
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)

    def apply_cors_headers(self) -> None:
        resp = Response(cors=True, cache=None)
        req = self._last_request()
        for name, value in response_headers(resp, req, self.server, self.command):
            if name.startswith("Access-Control") or name == "Vary":
                self.send_header(name, value)

    # -- request / response bridge -------------------------------------------
    def _last_request(self) -> Request:
        """The Request built for the current response (for compat helpers)."""
        req = getattr(self, "_req", None)
        if req is None:
            req = self._new_request()
        return req

    def _new_request(self) -> Request:
        ip = (self.client_address or ("unknown", 0))[0]
        return Request(self.command, self.path,
                       headers=Headers(self.headers.items()),
                       remote_ip=ip, rid=make_request_id())

    def _fill_body(self, req: Request) -> None:
        if self._is_multipart():
            req.fields, req.files = self._read_multipart()
        else:
            req.body = self._read_json()

    def _emit(self, resp: Response, req: Request, method: str) -> None:
        """Write a core Response to the socket. This is the only place in the
        stdlib path that touches response headers."""
        self._rid = req.rid
        try:
            self.send_response(resp.status)
            for name, value in response_headers(resp, req, self.server, method):
                self.send_header(name, value)
            self.end_headers()
            if resp.stream is None:
                self.wfile.write(resp.body)
            else:
                for chunk in resp.stream:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The browser went away before the response was written (page
            # refresh, tab closed, request aborted). The work — e.g. a
            # provider test — was already done and saved, so there is
            # nobody left to answer: drop it quietly instead of a traceback.
            self.close_connection = True

    def _dispatch(self, method: str) -> None:
        req = self._new_request()
        self._req = req
        try:
            self._fill_body(req)
        except ApiError as e:
            self._emit(api_error_response(e, req.rid), req, method)
            return
        self._emit(WebApp(self.server).handle(req), req, method)

    def do_GET(self):    return self._dispatch("GET")
    def do_POST(self):   return self._dispatch("POST")
    def do_PATCH(self):  return self._dispatch("PATCH")
    def do_DELETE(self): return self._dispatch("DELETE")

    def do_OPTIONS(self):
        req = self._new_request()
        self._req = req
        self._emit(WebApp(self.server).options(req), req, "OPTIONS")


class AstraServer(AstraSite, ThreadingHTTPServer):
    """The stdlib (zero-dependency) server: AstraSite + a threaded socket."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        # Client hung up mid-request (refresh / closed tab): not an error.
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)

    def __init__(self, addr, store, agent, stack=None):
        ThreadingHTTPServer.__init__(self, addr, AstraHandler)
        AstraSite.__init__(self, addr, store, agent, stack=stack)


if __name__ == "__main__":  # pragma: no cover - manual smoke entry point
    from .bootstrap import build
    stack = build()
    srv = AstraServer(("127.0.0.1", 8787), stack["store"], stack["agent"],
                      stack=stack)
    print(f"{AGENT_NAME} on http://127.0.0.1:8787/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
