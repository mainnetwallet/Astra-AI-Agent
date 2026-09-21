"""Astra's web server: the FastAPI/ASGI adapter over the shared router.

`astra.web` owns *what* a request means — the route table, operator auth,
rate limiting, CORS, security headers, secret redaction, uploads, the body
cap and the SSE live feed — and deals in neutral Request/Response objects.
This module owns *how* those bytes travel: it binds that router to
FastAPI/uvicorn through one catch-all route, a lifespan that builds and tears
down the stack, and a worker threadpool for blocking work. It contains no
route logic of its own; there is exactly one implementation of the API.

Run it:

    python3 run.py                                  # banner + env handling
    uvicorn --factory astra.web_fastapi:create_app      # plain ASGI deployment

Design notes:

  * `lifespan` — the stack is built on startup and the scheduler stopped plus
    the store closed on shutdown, so `uvicorn --factory` deployments have a
    single owner for the process lifecycle (see `make_app` for the exact
    ownership rules).
  * blocking work (agent turns, provider probes) runs in the anyio worker
    threadpool, so the event loop stays free. Size that pool with
    `ASTRA_ASGI_THREADS` if you run many concurrent turns.
  * the SSE live feed is driven by an async generator (`web.sse_frames`): an
    idle Live tab costs no worker thread, where a sync generator would occupy
    one of the 40 default pool slots for the whole 45s of the connection and
    could starve agent turns.
  * the body cap (`ASTRA_MAX_BODY_MB`) is also enforced for a chunked request
    that carries no Content-Length;
  * multipart is parsed by the shared stdlib parser, so `python-multipart` is
    not required;
  * static files, traversal rules and cache headers are served by the shared
    router rather than Starlette's StaticFiles, so `Cache-Control` and CSP
    stay in one place;
  * the documented addresses in astra/web.py are why the router is mounted as
    one catch-all instead of re-declared as FastAPI routes: a second
    declaration would be a copy that can fall out of sync.
    `ASTRA_FASTAPI_DOCS=1` still exposes /docs + /openapi.json, but with a
    catch-all the generated schema cannot describe individual routes —
    README's API table is the authoritative reference.

Importing this module never fails just because FastAPI is missing —
`make_app()` raises a clear RuntimeError instead of an ImportError from
somewhere deep in a call chain.
"""
from __future__ import annotations

import contextlib
import json
import os

from .security import make_request_id
from .web import (AGENT_NAME, AstraSite, Headers, Request, Response,
                  WebApp, check_body_size, error_response,
                  parse_multipart_body, response_headers)

__all__ = ["make_app", "create_app", "AGENT_NAME", "HAVE_FASTAPI"]

# Upper bound on how much of a rejected request body we are willing to drain
# so the client can read our 413 (see _discard_request_body).
_MAX_DRAIN_BYTES = 64 * 1024 * 1024

try:  # stay importable even when FastAPI is missing
    from fastapi import FastAPI, Request as ASGIRequest
    from fastapi.responses import Response as ASGIResponse, StreamingResponse
    from starlette.concurrency import run_in_threadpool

    HAVE_FASTAPI = True
    _IMPORT_ERROR: "Exception | None" = None
except ImportError as exc:  # pragma: no cover - depends on the extra
    FastAPI = ASGIRequest = ASGIResponse = StreamingResponse = None
    run_in_threadpool = None
    HAVE_FASTAPI = False
    _IMPORT_ERROR = exc


class _AppState:
    """The site/router pair, plus whatever we are responsible for closing.

    Built eagerly when the caller already has the pieces (tests, run.py),
    lazily inside the ASGI lifespan when nothing was supplied — so
    `uvicorn --factory` never builds a stack at import time.
    """

    def __init__(self, site=None, stack=None, store=None, agent=None):
        self.site = site
        self.stack = stack
        self.store = store
        self.agent = agent
        self.router = WebApp(site) if site is not None else None
        # nothing supplied at all -> we build (and therefore own) the stack
        self.lazy = site is None and stack is None and store is None \
            and agent is None

    def ensure(self) -> WebApp:
        if self.router is None:
            if self.site is None:
                if self.stack is None:
                    from .bootstrap import build
                    self.stack = build()
                self.site = AstraSite(("127.0.0.1", 0),
                                      self.store or self.stack["store"],
                                      self.agent or self.stack["agent"],
                                      stack=self.stack)
            self.router = WebApp(self.site)
        return self.router


def _asgi_headers(resp: Response, req: Request, site: AstraSite, method: str) -> dict:
    """Core header list -> plain mapping for Starlette.

    Content-Length is dropped and recomputed by Starlette so the two servers
    cannot disagree about the framing of a body they both produced.
    """
    out = {}
    for name, value in response_headers(resp, req, site, method):
        if name.lower() == "content-length":
            continue
        out[name] = value
    return out


async def _discard_request_body(request: ASGIRequest, budget: int) -> None:
    """Read and throw away what the client is still sending.

    The body cap is enforced from Content-Length *before* any byte is
    buffered, but if we then answer without reading the request, uvicorn
    closes the socket while the client is still writing the body and the
    client sees a connection reset instead of our 413. Draining the declared
    bytes lets the client finish its write and read the response; `budget`
    keeps a lying client from making us read forever.
    """
    remaining = budget
    try:
        async for chunk in request.stream():
            remaining -= len(chunk)
            if remaining <= 0:
                break
    except Exception:
        pass


def _render(resp: Response, req: Request, site: AstraSite, method: str):
    headers = _asgi_headers(resp, req, site, method)
    if resp.stream is not None:
        # async drive: no worker thread is held while the feed is idle
        return StreamingResponse(resp.stream, status_code=resp.status,
                                 headers=headers)
    return ASGIResponse(content=resp.body, status_code=resp.status,
                        headers=headers)


def _apply_thread_limit() -> None:
    """Optional sizing of the worker threadpool that runs blocking calls.

    The anyio/Starlette default is 40. Agent turns, provider probes and provider
    tests are all long blocking calls, so a busier deployment can want more;
    `ASTRA_ASGI_THREADS=0` (the default) keeps Starlette's own default.
    """
    raw = os.environ.get("ASTRA_ASGI_THREADS", "0") or "0"
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return
    if limit <= 0:
        return
    try:
        from anyio import to_thread
        to_thread.current_default_thread_limiter().total_tokens = limit
    except Exception:  # pragma: no cover - anyio always ships with starlette
        pass


class _AstraASGI:
    """ASGI <-> router translation for one app.

    Kept as a class (rather than closures) so the annotations on `dispatch`
    resolve from module scope, which is what FastAPI's signature
    introspection needs.
    """

    def __init__(self, state: _AppState):
        self.state = state

    def build_request(self, asgi_request: ASGIRequest, method: str) -> Request:
        # Use scope["raw_path"], not url.path: uvicorn (correctly) percent-
        # decodes scope["path"], but the router must see an encoded "/" the
        # way the client sent it — a model id like "openai%2Fgpt-oss-120b"
        # has to arrive as ONE path segment, not two. scope["raw_path"]
        # carries the exact bytes the client put on the wire.
        scope = asgi_request.scope
        raw = scope.get("raw_path")
        if raw:
            raw_path = raw.decode("latin-1") if isinstance(raw, (bytes, bytearray)) \
                else str(raw)
        else:  # non-uvicorn ASGI servers may omit raw_path
            raw_path = asgi_request.url.path
        query = asgi_request.url.query
        if query:
            raw_path += "?" + query
        client = asgi_request.client
        # uvicorn's --proxy-headers rewrites scope["client"] from
        # X-Forwarded-For, so a reverse-proxied deployment keys rate limiting
        # on the real client once the operator opts in there; by default this
        # is the direct peer, which cannot be spoofed.
        return Request(method, raw_path,
                       headers=Headers(asgi_request.headers.items()),
                       remote_ip=(client.host if client else "unknown"),
                       rid=make_request_id())

    async def dispatch(self, asgi_request: ASGIRequest, method: str):
        if self.state.router is None:  # first request before startup finished
            await run_in_threadpool(self.state.ensure)
        site = self.state.site
        router = self.state.router
        req = self.build_request(asgi_request, method)

        # Reject an oversized body before buffering a single byte of it.
        content_length = req.headers.get("Content-Length")
        too_big = check_body_size(site, content_length)
        if too_big is not None:
            try:
                declared = int(content_length or 0)
            except (TypeError, ValueError):
                declared = 0
            await _discard_request_body(asgi_request,
                                        min(declared, _MAX_DRAIN_BYTES))
            return _render(too_big, req, site, method)

        if method == "OPTIONS":
            return _render(router.options(req), req, site, method)

        raw = await asgi_request.body()
        # A chunked request sends no Content-Length, so the pre-flight check
        # above cannot see it: enforce the same cap on what actually arrived.
        if len(raw) > site.max_body_bytes:
            return _render(error_response("payload_too_large", 413,
                                          "payload_too_large", req.rid),
                           req, site, method)
        content_type = req.headers.get("Content-Type", "")
        if "multipart/form-data" in content_type:
            req.fields, req.files = parse_multipart_body(raw, content_type)
        elif raw:
            try:
                req.body = json.loads(raw.decode("utf-8"))
            except Exception:
                req.body = {}
        if not isinstance(req.body, dict):
            req.body = {}

        # The router does blocking work (agent turn, provider probes) — run it
        # off the event loop so a slow turn cannot stall the SSE live feed.
        resp = await run_in_threadpool(router.handle, req)
        return _render(resp, req, site, method)


def make_app(stack=None, store=None, agent=None, site=None, docs=None,
             manage_lifecycle=None):
    """Build the ASGI app around the shared router.

    Exactly one owner for the stack, chosen by what you pass:

      * nothing — the app builds the normal bootstrap stack on ASGI startup
        and stops the scheduler + closes the store on shutdown (this is what
        `uvicorn --factory astra.web_fastapi:create_app` does);
      * `stack=` — used as-is; lifecycle defaults to managed (the app shuts
        down the scheduler/store), matching run.py;
      * `store=`/`agent=` — the caller owns the lifecycle;
      * `site=` — a ready-made AstraSite, the caller owns everything (tests).

    Pass `manage_lifecycle=True/False` to override that default.
    """
    if not HAVE_FASTAPI:
        raise RuntimeError(
            "FastAPI/uvicorn are required. Install them with:\n"
            "\n"
            "    pip install -r requirements.txt\n"
            "\n"
            "Termux/Android: pydantic 2 has no Android wheel, so install the\n"
            "pure-Python path instead:\n"
            "    pip install \"fastapi<0.119\" \"uvicorn>=0.27\" \"pydantic<2\"\n"
        ) from _IMPORT_ERROR

    state = _AppState(site=site, stack=stack, store=store, agent=agent)
    if manage_lifecycle is None:
        manage_lifecycle = state.lazy or stack is not None
    state.ensure()  # eager when we were handed pieces; no-op when lazy

    if docs is None:
        docs = os.environ.get("ASTRA_FASTAPI_DOCS") == "1"

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await run_in_threadpool(state.ensure)
        _apply_thread_limit()
        try:
            yield
        finally:
            if manage_lifecycle and state.stack is not None:
                scheduler = state.stack.get("scheduler")
                if scheduler is not None:
                    try:
                        scheduler.stop()
                    except Exception:
                        pass
                browser = state.stack.get("browser_manager")
                if browser is not None:
                    try:
                        browser.close_all()
                    except Exception:
                        pass
                try:
                    state.stack["store"].close()
                except Exception:
                    pass

    app = FastAPI(
        title=AGENT_NAME,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    bridge = _AstraASGI(state)

    async def catch_all(fastapi_request: ASGIRequest, full_path: str = ""):
        """One catch-all: every address the router does not recognise comes
        back as a structured 404."""
        return await bridge.dispatch(fastapi_request, fastapi_request.method)

    # Registered once per method (rather than one route with methods=[...])
    # so each gets its own operation_id. FastAPI derives a route's default
    # operation_id from only the first entry of route.methods, so a single
    # multi-method route makes every method collide on the same
    # operation_id in the generated OpenAPI schema (spurious "Duplicate
    # Operation ID" warnings, and broken per-operation IDs for anything
    # that reads /openapi.json, e.g. Swagger UI or client codegen).
    for _method in ("GET", "POST", "PATCH", "DELETE", "OPTIONS"):
        app.api_route("/{full_path:path}", methods=[_method])(catch_all)

    return app


def create_app():
    """Zero-argument factory for `uvicorn --factory astra.web_fastapi:create_app`.

    The stack is built on startup (never at import) and torn down on shutdown.
    """
    return make_app()
