"""Astra AI Gateway — a separate multi-service AI gateway with automatic fallback.

Replaces the old third-party AI gateway service with a set of ten
independent AI connections:

    Astra AI Gateway
    ├── Gemini      ← GW_GEMINI_* config
    ├── Groq        ← GW_GROQ_* config
    ├── Cloudflare  ← GW_CLOUDFLARE_* config
    ├── Bedrock     ← GW_BEDROCK_* config
    ├── OpenRouter  ← GW_OPENROUTER_* config
    ├── Mistral     ← GW_MISTRAL_* config
    ├── Cerebras    ← GW_CEREBRAS_* config
    ├── SambaNova   ← GW_SAMBANOVA_* config (GW_SAMBA_* also accepted)
    ├── Cohere      ← GW_COHERE_* config
    └── Z.AI (GLM)  ← GW_ZAI_* config

Each connection has completely independent credentials, models and
endpoints/base URLs — separate from the existing Provider system:
GW_GEMINI_API_KEYS ≠ GEMINI_API_KEYS, etc. The Gateway is NOT a provider:
it is never added to ProviderRegistry and is reported separately in
health/dashboard output, never inside the provider table.

Architecture note: this module is fully self-contained. It does NOT import,
subclass, or instantiate the existing Provider adapter classes (the ten
modules in astra/ai/adapters/*) — those remain exclusively the Provider
system's. Each Gateway connection below implements its own request/response/
auth plumbing against its own GW_-prefixed configuration. Nothing here reads
GEMINI_*/GROQ_*/… or touches a Provider adapter instance, client, or config
object.

Fallback: connections are attempted in `GATEWAY_CONNECTIONS` order — the
original Gemini → Groq → Cloudflare → Bedrock chain first, then the six
additional connections — with per-model health/cooldown and capability
scoring applied on top (see astra/ai/gateway_routing.py). A failure at any
connection (or model) automatically moves to the next healthy target. The
same task, messages, context and system instructions carry over unchanged
between services (the full request is re-sent with the next connection until
one succeeds) — no restart, no task duplication.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from astra.ai.credentials import CredentialPool
from astra.ai.image_payload import image_result_to_data_uri
from astra.ai.system_prompt import build_system_prompt
from astra.core.exceptions import ProviderError, TimeoutError
from astra.core.events import new_op_id
from astra.ai.shared_health import SharedHealthCoordinator, resolve_identity


def _close_http_error(exc) -> None:
    """Release an HTTPError's response body (urlopen raises before the `with`
    body runs, so the socket would otherwise stay open until GC)."""
    try:
        close = getattr(exc, "close", None)
        if close is not None:
            close()
    except Exception:
        pass


def _short_provider(conn) -> str:
    """Short display id of a Gateway connection (astra-gw-groq -> groq), the
    same naming the routed path and the Logs panel use."""
    from astra.ai.gateway_routing import GATEWAY_PROVIDER_SHORT
    name = getattr(conn, "name", "")
    return GATEWAY_PROVIDER_SHORT.get(name, name)

GW_DEFAULT_TIMEOUT = 60
GW_STREAM_TIMEOUT = 120
# Bounded transient-failure retry for ONE connection attempt (a network
# blip / 5xx / 429 is worth one more try against the pool; auth and
# request-level errors are not retried at all). This never replaces the
# Gateway's own per-target fallback: when retries are exhausted the error
# propagates and the attempt loop moves on to the next healthy target.
GW_MAX_RETRIES = 1
GW_RETRY_BACKOFF = 0.5
GW_RETRY_MAX_BACKOFF = 8.0


def _is_timeout(exc) -> bool:
    """True for both a socket timeout and an Astra TimeoutError."""
    return isinstance(exc, socket.timeout) or "timed out" in str(exc).lower()


# ── Activity Log input/output (astra_gateway.* events only) ─────────────────
# `static/js/log_model.js` renders any event's `data.input`/`data.output`
# string fields as an "Input"/"Output" block in the expanded log row. The
# Gateway sends the COMPLETE request (every message, each labelled with its
# role) and the COMPLETE response so the Activity Log shows exactly what went
# into and came out of an API call. The only cap is a large safety limit
# (per field, env-overridable) that keeps a pathological payload from
# bloating the events table / SSE stream; a truncated field says so.
try:
    GW_LOG_MAX_CHARS = max(1000, int(os.environ.get("ASTRA_LOG_MAX_CHARS", "200000")))
except ValueError:
    GW_LOG_MAX_CHARS = 200000


def _gw_log_cap(text: str) -> str:
    if len(text) <= GW_LOG_MAX_CHARS:
        return text
    return (text[:GW_LOG_MAX_CHARS]
            + f"\n… [truncated {len(text) - GW_LOG_MAX_CHARS} more characters]")


def _gw_log_content(content) -> str:
    """Flatten one message's content to text. Multimodal parts keep their
    text; binary parts (images/audio/files, often base64) are replaced by a
    short placeholder so the log never carries raw media."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                else:
                    parts.append(f"[{part.get('type') or 'non-text'} content omitted]")
            else:
                parts.append(str(part))
        return "\n".join(parts)
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _gw_log_input(messages) -> str:
    """The complete input of a Gateway call for the Activity Log: every
    message in order, each prefixed with its role. Never raises."""
    try:
        blocks = []
        for m in messages or []:
            if isinstance(m, dict):
                role = str(m.get("role") or "message")
                text = _gw_log_content(m.get("content")).strip()
            else:
                role, text = "message", str(m).strip()
            if text:
                blocks.append(f"[{role}]\n{text}")
        return _gw_log_cap("\n\n".join(blocks))
    except Exception:
        return ""


def _gw_log_output(text) -> str:
    """The complete output text of a call, for the Activity Log."""
    try:
        return _gw_log_cap(str(text or "").strip())
    except Exception:
        return ""


# ── shared OpenAI-compatible plumbing (Gateway-only; not the Provider base) ──
# Gemini, Groq and Cloudflare all speak the same `{model, messages, max_tokens}`
# → `choices[].message.content` dialect. This base is private to the Gateway
# module — it does not derive from astra.ai.adapters.base.CompatibleAdapter or
# astra.ai.provider.AIProvider, and every connection built on it reads only
# its own GW_* env vars via its own CredentialPool instance.
class _GatewayCompatibleConnection:
    name: str = "astra-gw-compatible"
    models_env: str = ""
    api_keys_env: str = ""
    base_url_env: str = ""
    image_models_env: str = ""
    #: Image generation uses its OWN dedicated credentials/base URL,
    #: ISOLATED from the connection's normal chat pool (``api_keys_env``/
    #: ``base_url_env``). Set per-connection to the documented ``IMAGE_*``
    #: env names (e.g. ``IMAGE_GEMINI_API_KEY``). When the dedicated
    #: ``IMAGE_*`` credential is not configured, image generation for this
    #: provider is SKIPPED (see `image_pool` in `__init__` /
    #: `image_credentials_configured`) -- it never borrows the connection's
    #: chat ``GW_*`` credential/base URL.
    image_api_keys_env: str = ""
    image_base_url_env: str = ""
    base_url: str = ""
    capabilities: list[str] = ["chat", "stream"]
    #: Image-generation model ids, configured SEPARATELY from the chat list
    #: (an image model is not a chat model). Never inferred from the id --
    #: astra.ai.image_models + live discovery decide what may run.
    image_models: list[str] = []
    # Static per-request headers a provider's API expects/accepts (e.g.
    # OpenRouter's optional attribution headers). Secret-free by construction —
    # credentials are always added by `_headers()` from the pool.
    extra_headers: dict = {}
    # Optional alternate env names, consulted ONLY when the primary env var is
    # empty (e.g. SambaNova's short `GW_SAMBA_*` prefix next to the canonical
    # `GW_SAMBANOVA_*`). Opt-in per connection; unset for the rest.
    env_aliases: dict = {}

    def __init__(self, config=None, events=None, pool: CredentialPool | None = None):
        self.config = config
        self.events = events
        keys_env = self._first_env(self.api_keys_env)
        self.pool = pool or CredentialPool.from_env(config, keys_env, self.name)
        self.models = (self._env_list(self.models_env) if config and self.models_env
                       else list(self.models if hasattr(self, "models") else []))
        self.image_models = (self._env_list(self.image_models_env)
                             if config and self.image_models_env
                             else list(getattr(type(self), "image_models", []) or []))
        raw = self._env_str(self.base_url_env)
        self.base_url = (raw or self.base_url or "").rstrip("/")
        # -- dedicated image-generation credentials/base URL (see class doc) --
        # A dedicated IMAGE_* key gets its OWN CredentialPool, isolated from
        # the connection's normal chat `self.pool` (a rejected image key
        # never marks the chat pool unhealthy, and vice versa). When no
        # dedicated IMAGE_* key is configured, `self.image_pool` is an EMPTY
        # pool (never `self.pool`): image generation for this provider must
        # be SKIPPED, not silently run on the chat GW_* credential. See
        # `image_credentials_configured()` / `_run`'s `pool` handling, which
        # treats an explicitly-empty pool as "no image credential" rather
        # than falling through to the chat pool.
        img_secrets = (self._env_list(self.image_api_keys_env)
                       if config and self.image_api_keys_env else [])
        img_label = (self._first_env(self.image_api_keys_env)
                    or f"{self.name}-image")
        self.image_pool = CredentialPool(img_label, img_secrets)
        raw_img_base = (self._env_str(self.image_base_url_env)
                        if self.image_base_url_env else None)
        self.image_base_url = (raw_img_base or "").rstrip("/")
        # Thread-local "which pool is this call using" — set by `_run()` for
        # the duration of one request so `_pick()`/`_done()` (called deep
        # inside `_post`/`_classify_http`) route success/failure to the
        # correct pool (chat vs. dedicated image) without changing every
        # helper's signature.
        self._tl_pool = threading.local()
        self._last_usage: dict = {}
        self._last_usage_stream: dict = {}
        # The most recent CREDENTIAL-level rejection (HTTP 401/403) seen by
        # this connection. A dead token is a property of the CONNECTION, not
        # of any one model: once the pool has no usable key left, every later
        # call re-reports this real error (with its real status code) instead
        # of inventing a fresh per-model "failure" for models that were never
        # actually sent to the provider. See `_credential_error` / `_run`.
        self._last_auth_error: ProviderError | None = None
        self.max_retries = max(0, int(
            config.getint("GW_MAX_RETRIES", GW_MAX_RETRIES)
            if config is not None and hasattr(config, "getint") else GW_MAX_RETRIES))
        try:
            self.retry_backoff = float(
                config.get("GW_RETRY_BACKOFF", GW_RETRY_BACKOFF)
                if config is not None else GW_RETRY_BACKOFF)
        except (TypeError, ValueError):
            self.retry_backoff = GW_RETRY_BACKOFF

    # -- env resolution (primary name, then any documented alias) ------------
    def _env_names(self, name: str) -> tuple[str, ...]:
        if not name:
            return ()
        return (name,) + tuple(self.env_aliases.get(name, ()))

    def _first_env(self, name: str) -> str:
        """First alias name that actually has a value configured, else the
        primary name. Only used to label the credential pool."""
        for n in self._env_names(name):
            if self.config is not None and self._env_list(n):
                return n
        return name

    def _env_list(self, name: str) -> list:
        for n in self._env_names(name):
            vals = (self.config.getlist(n, default=[]) if self.config else []) or []
            if vals:
                return vals
        return []

    def _env_str(self, name: str):
        for n in self._env_names(name):
            v = (self.config.get(n, None) if self.config else None)
            if v:
                return v
        return None

    # -- credentials ------------------------------------------------------
    def _active_pool(self):
        """The pool THIS thread's current `_run()` call is using — the
        dedicated image pool (even when it is empty, i.e. no IMAGE_*
        credential configured) while an image-generation call is in
        flight, else the normal chat pool. See `image_pool` in `__init__`.

        Deliberately an ``is not None`` check, NOT ``... or self.pool``: an
        explicitly-empty image pool must stay empty here, or a falsy-but-
        real pool object would silently resolve back to the chat pool --
        exactly the IMAGE_*→GW_* fallback this isolation forbids."""
        tl = getattr(self._tl_pool, "pool", None)
        return tl if tl is not None else self.pool

    def _pick(self):
        return self._active_pool().pick()

    def _credential_error(self):
        """The REAL credential rejection for this connection, when the pool
        has keys but none is usable — else None.

        A rejected/revoked API token (HTTP 401/403) is a CONNECTION-level
        failure, not a per-model one: returning the cached error keeps its
        true status code and message, so the Activity Log never reports a
        model as "failed" for a call that was never made. Falls back to None
        (the caller's generic "no healthy credential" error) when the pool is
        empty or the keys are merely cooling down after a transient error —
        there is no real credential rejection to report then.
        """
        cached = getattr(self, "_last_auth_error", None)
        if cached is None:
            return None
        pool = self._active_pool()
        count = getattr(pool, "count", 0)
        if not count or bool(pool):
            return None
        return cached

    def _done(self, cred=None, errored=False, reason="", *, rate_limited=False,
              auth_failure=False, cooldown_s: float = 30.0) -> None:
        if cred is None:
            return
        pool = self._active_pool()
        if errored:
            pool.report_failure(cred, reason=reason, rate_limited=rate_limited,
                                auth_failure=auth_failure, cooldown_s=cooldown_s)
        else:
            pool.report_success(cred)

    def _headers(self, cred) -> dict:
        h = {"content-type": "application/json", **dict(self.extra_headers)}
        secret = self.pool.get_secret_for(cred) if cred else ""
        if secret:
            h["Authorization"] = f"Bearer {secret}"
        return h

    # -- HTTP ---------------------------------------------------------------
    def _post(self, url: str, body: dict, cred) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(cred))
        try:
            with urllib.request.urlopen(req, timeout=GW_DEFAULT_TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            _close_http_error(e)
            self._classify_http(e, cred)
            raise
        except OSError as e:
            self._done(cred, True, reason=f"network: {getattr(e, 'reason', e)}")
            if _is_timeout(e):
                err = TimeoutError(f"{self.name} timed out")
                err.retryable = True
                raise err from e
            err = ProviderError(
                f"{self.name} network error: {getattr(e, 'reason', e)}")
            err.retryable = True
            raise err from e
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as e:
            err = ProviderError(f"{self.name} bad json response")
            err.retryable = False
            raise err from e


    def _post_with_headers(self, url, body, cred, headers):
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers)
        return self._read_json(req, cred)

    def _post_raw(self, url, body, cred) -> bytes:
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=self._headers(cred))
        return self._read_raw(req, cred)

    def _read_raw(self, req, cred) -> bytes:
        try:
            with urllib.request.urlopen(req, timeout=GW_DEFAULT_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            _close_http_error(e)
            self._classify_http(e, cred)
            raise
        except OSError as e:
            self._done(cred, True, reason="network: %s" % getattr(e, "reason", e))
            if _is_timeout(e):
                err = TimeoutError(self.name + " timed out")
            else:
                err = ProviderError(self.name + " network error")
            err.retryable = True
            raise err from e

    def _read_json(self, req, cred) -> dict:
        raw = self._read_raw(req, cred)
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as e:
            err = ProviderError(self.name + " bad json response")
            err.retryable = False
            raise err from e

    # -- bounded transient retry (per connection attempt) -------------------
    def _attempt_count(self) -> int:
        return max(0, int(getattr(self, "max_retries", GW_MAX_RETRIES))) + 1

    def _run(self, fn, *, pool: CredentialPool | None = None,
             single_attempt: bool = False):
        """Run `fn(cred)` with a bounded number of attempts.

        A fresh credential is picked for every attempt, so a 429/auth
        failure rotates across the pool exactly as the non-retrying path
        always did. Only errors explicitly marked `retryable` (network blip,
        timeout, 429, 5xx) are retried; auth and request-level errors (400/
        404/409/422) propagate immediately so the Gateway's own fallback
        moves to the next connection without pointless extra latency. When
        the retry cannot even get a credential (all keys cooled down) the
        original, informative error is re-raised rather than a generic
        "no healthy credential" — failover reporting stays truthful.

        ``single_attempt``: make EXACTLY one provider attempt, with no
        same-model retry even for a retryable 429/5xx. Image generation sets
        this: its serial fallback gives each (provider, model) exactly one
        attempt per user request, so a rate limit must advance to the NEXT
        image model rather than spend extra quota on the same one.

        ``pool``: which CredentialPool `_pick()`/`_done()` should use for the
        duration of this call (defaults to the connection's normal chat
        pool when ``None``). Image generation passes `self.image_pool` so a
        dedicated IMAGE_* credential never borrows/consumes the chat pool's
        health state, and vice versa -- including when `self.image_pool` is
        an EMPTY pool (no IMAGE_* configured): that must raise "no healthy
        credential" below, not fall through to the chat pool, so this uses
        ``pool if pool is not None else self.pool`` rather than
        ``pool or self.pool`` (an empty pool is falsy but still a real,
        deliberate choice, not "unset")."""
        prev = getattr(self._tl_pool, "pool", None)
        self._tl_pool.pool = pool if pool is not None else self.pool
        try:
            attempts = 1 if single_attempt else self._attempt_count()
            last = None
            for attempt in range(attempts):
                cred = self._pick()
                if cred is None:
                    if last is not None:
                        raise last
                    auth = self._credential_error()
                    if auth is not None:
                        raise auth
                    err = ProviderError(
                        f"{self.name}: no healthy credential configured")
                    err.retryable = False
                    raise err
                try:
                    return fn(cred)
                except (ProviderError, TimeoutError) as e:
                    last = e
                    if attempt + 1 >= attempts or not getattr(e, "retryable", False):
                        raise
                    time.sleep(min(self.retry_backoff * (attempt + 1),
                                   GW_RETRY_MAX_BACKOFF))
            raise last
        finally:
            self._tl_pool.pool = prev

    def _image_api_base(self) -> str:
        """Base URL for an image-generation request: the dedicated
        ``IMAGE_*_BASE_URL`` when configured, else this provider's
        hardcoded documented image-API default (the class's own
        ``base_url`` attribute, read directly off the class -- NEVER the
        instance's ``self.base_url``/``self._api_base()``, which may have
        been overridden by the normal chat ``GW_*_BASE_URL``). A missing
        ``IMAGE_*_BASE_URL`` must never resolve to a customized chat base
        URL."""
        return (self.image_base_url or type(self).base_url or "").rstrip("/")

    def _api_base(self) -> str:
        """Base URL for ONE request (see CompatibleAdapter._api_base): connections
        needing a per-request base override this instead of mutating the
        shared ``self.base_url`` from concurrent threads."""
        return self.base_url

    def _classify_http(self, e: urllib.error.HTTPError, cred) -> None:
        code = getattr(e, "code", 0)
        rate_limited = code in (408, 429)
        auth = code in (401, 403)
        # request/model-level errors must not cool down the shared API key
        request_level = code in (400, 404, 409, 422)
        self._done(cred, True, reason=f"http {code}", rate_limited=rate_limited,
                   auth_failure=auth,
                   cooldown_s=(0.0 if request_level else 45 if rate_limited else 30))
        # Only transient upstream conditions are worth a second attempt from
        # the same connection; everything else fails over immediately.
        retryable = code in (408, 429, 500, 502, 503, 504)
        if code == 408:
            err = TimeoutError(f"{self.name} timed out")
            err.retryable = True
        elif code == 429:
            err = ProviderError(f"{self.name} rate limit reached")
            err.retryable = True
            err.rate_limited = True
        elif code in (401, 403):
            err = ProviderError(f"{self.name} authentication failed")
            err.retryable = False
        else:
            err = ProviderError(f"{self.name} http {code}")
            err.retryable = retryable
        # Attach the real HTTP status so the Activity Log can report the
        # provider API's status code without parsing the message text (the
        # image-generation API-call logging reads `err.code`).
        err.code = int(code)
        if code in (401, 403):
            self._last_auth_error = err
        raise err

    def _read_sse(self, resp) -> list[dict]:
        results = []
        raw = resp.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        for line in raw.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                results.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
        return results

    # -- interface ------------------------------------------------------------
    def chat(self, messages, model=None, max_tokens=None) -> str:
        body = {"model": model or (self.models[0] if self.models else ""),
                "messages": messages}
        # Output limit is optional for these APIs: an unset budget is omitted
        # so the model uses its own maximum (astra.ai.token_limits).
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        def once(cred):
            data = self._post(f"{self._api_base()}/chat/completions", body, cred)
            self._done(cred)
            return data

        data = self._run(once)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            text = ""
        self._last_usage = data.get("usage", {})
        return text.strip() or "(no reply)"

    def stream(self, messages, model=None, max_tokens=None):
        model_id = model or (self.models[0] if self.models else "")
        if self.events:
            self.events.emit("ai.started", agent="gateway", provider=self.name,
                             model=model_id)
        body = {"model": model_id, "messages": messages, "stream": True}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        payload = json.dumps(body).encode("utf-8")

        def connect(cred):
            """Open the SSE response. `urlopen` returns only after the status
            line + headers, so auth/rate-limit/5xx still raise HERE and are
            retried/failed over before any token has been yielded."""
            req = urllib.request.Request(
                f"{self._api_base()}/chat/completions",
                data=payload, headers=self._headers(cred))
            try:
                return cred, urllib.request.urlopen(req, timeout=GW_STREAM_TIMEOUT)
            except urllib.error.HTTPError as e:
                _close_http_error(e)
                self._classify_http(e, cred)
                raise
            except OSError as e:
                self._done(cred, True, reason="stream network error")
                err = ProviderError(f"{self.name} stream network error")
                err.retryable = True
                raise err from e

        # Only the CONNECT phase is retried: once the first chunk is yielded a
        # retry would duplicate output, so a mid-stream failure propagates and
        # the caller/Gateway decides what to do next.
        cred, resp = self._run(connect)
        full = ""
        try:
            with resp:
                for chunk in self._read_sse(resp):
                    usage = chunk.get("usage")
                    if usage:
                        # OpenAI-compatible streams include usage in the final
                        # frame only when the upstream sends it — preserve it
                        # when present, never invent it.
                        self._last_usage_stream = usage
                    choices = chunk.get("choices", [])
                    delta = choices[0].get("delta", {}) if choices else {}
                    text = delta.get("content", "")
                    if text:
                        full += text
                        yield text
        except OSError as e:
            self._done(cred, True, reason="stream interrupted")
            err = ProviderError(f"{self.name} stream interrupted")
            err.retryable = False
            raise err from e
        self._done(cred)
        if self.events:
            self.events.emit("ai.completed", agent="gateway", provider=self.name,
                             length=len(full))

    # -- image generation -----------------------------------------------------
    def _default_image_model(self) -> str:
        return (self.image_models[0] if self.image_models else "")

    def list_image_models(self, *, discover: bool = True) -> list:
        """Image-generation models this connection can serve.

        Default: the configured list. Connections with an official image-model
        discovery API (OpenRouter) override this to merge LIVE results, which
        are authoritative for the exact model ids they return.
        """
        return list(self.image_models)

    def generate_image(self, prompt: str, model: str | None = None,
                       size: str = "1024x1024", n: int = 1) -> str:
        """Generate an image via the OpenAI-compatible Images API
        (`POST /images/generations` + `response_format=b64_json`), used by
        connections whose documented image protocol this is (Z.AI
        GLM-Image/CogView). Connections with a different documented protocol
        override this method, so protocol ownership is explicit: OpenRouter
        (``POST /api/v1/images``), Gemini (native ``:generateContent``),
        Cloudflare (Workers AI ``/ai/run/<model>``) and Bedrock
        (``InvokeModel``).
        Returns `data:<mime>;base64,<...>`; a 429/5xx/network error
        propagates so the Gateway fails over to the next image target.
        """
        model = model or self._default_image_model()
        if not model:
            raise ProviderError(f"{self.name}: no image model configured")
        body = {"model": model, "prompt": prompt, "n": max(1, int(n or 1)),
                "size": size, "response_format": "b64_json"}

        def once(cred):
            data = self._post(f"{self._image_api_base()}/images/generations",
                              body, cred)
            self._done(cred)
            return data

        data = self._run(once, pool=self.image_pool, single_attempt=True)
        uri = image_result_to_data_uri(data)
        if not uri:
            err = ProviderError(
                f"{self.name}: image generation returned no image data")
            err.retryable = False
            raise err
        return uri

    def health_check(self) -> bool:
        return bool(self.pool)

    def last_usage(self) -> dict:
        """Non-secret token/usage metadata of the most recent call, when the
        provider reported any ({} otherwise). Streaming usage is reported by
        OpenAI-compatible APIs only in the final SSE frame, and only when the
        upstream includes it — never invented here."""
        return dict(self._last_usage_stream or self._last_usage or {})

    def credential_summary(self) -> dict:
        return self.pool.summary()

    def image_credentials_configured(self) -> bool:
        """True only when this connection's OWN dedicated IMAGE_* credential
        is configured. Used by `astra.ai.gateway_routing.
        eligible_image_generation_targets` to decide provider eligibility for
        image generation, BEFORE any API call is attempted — the normal chat
        `self.pool` is never consulted here (see `image_api_keys_env`/
        `image_pool` above: `image_pool` is an empty, falsy pool when no
        IMAGE_* key is configured, never a copy of `self.pool`). Missing
        IMAGE_* credentials mean this provider is SKIPPED for image
        generation, never routed to the chat GW_* pool. Connections with an
        additional required credential (e.g. Cloudflare's account id)
        override this."""
        return bool(self.image_pool)

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


# ── Gateway connections ─────────────────────────────────────────────────────
# Each service has its own credentials / model list / endpoint, read only
# from its own GW_-prefixed env vars — completely independent of the
# existing Provider system's adapters, config and clients.

class AstraGatewayGemini(_GatewayCompatibleConnection):
    """Astra AI Gateway / Gemini connection (independent of GeminiAdapter)."""
    name = "astra-gw-gemini"
    #: Canonical, non-``GW_``-prefixed image-model list, owned by
    #: ImageRouter (astra/ai/image_router.py) — shared with the
    #: Provider/ModelRegistry system's own catalog seed (astra/ai/models.py),
    #: since both hold the identical documented FREE model id.
    image_models_env = "GEMINI_IMAGE_MODELS"
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    models_env = "GW_GEMINI_MODELS"
    api_keys_env = "GW_GEMINI_API_KEYS"
    base_url_env = "GW_GEMINI_BASE_URL"
    image_api_keys_env = "IMAGE_GEMINI_API_KEY"
    image_base_url_env = "IMAGE_GEMINI_BASE_URL"
    capabilities = ["chat", "stream", "tools", "json", "vision"]

    # ── image generation (native :generateContent) ────────────────────────
    native_base_url = "https://generativelanguage.googleapis.com/v1beta"

    def _native_base(self) -> str:
        # Dedicated IMAGE_GEMINI_BASE_URL only (see `image_base_url_env`) --
        # NEVER the chat GW_GEMINI_BASE_URL/self._api_base() (which may be
        # overridden for chat only and would be the wrong endpoint shape for
        # the native image API besides). When IMAGE_GEMINI_BASE_URL is
        # unset, falls back to this class's hardcoded native-image-API
        # default (`native_base_url`), never to a customized chat base URL.
        base = (self.image_base_url or "").rstrip("/")
        if base.endswith("/openai"):
            base = base[: -len("/openai")]
        return base or self.native_base_url

    @staticmethod
    def _inline_image(data) -> str:
        import base64 as _b64
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            return ""
        for part in parts or []:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if not inline or not inline.get("data"):
                continue
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            try:
                raw = _b64.b64decode(inline["data"])
            except Exception:
                continue
            return f"data:{mime};base64," + _b64.b64encode(raw).decode("ascii")
        return ""

    def generate_image(self, prompt: str, model: str | None = None,
                       size: str = "1024x1024", n: int = 1,
                       source_image: dict | None = None,
                       mask_image: dict | None = None) -> str:
        """Generate or edit an image through Gemini's native content API.

        Editing is capability-gated by ImageRouter, so a source image is only
        accepted here for a model explicitly marked as image-edit capable.
        """
        model = model or self._default_image_model()
        if not model:
            raise ProviderError(f"{self.name}: no image model configured")
        if mask_image is not None:
            raise ProviderError(f"{self.name}: inpainting is not supported")
        parts = [{"text": prompt}]
        if source_image:
            path = str(source_image.get("storage_path") or "")
            if not path or not os.path.isfile(path):
                raise ProviderError(f"{self.name}: source image is unavailable")
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
                if not raw:
                    raise ValueError("empty image")
            except Exception as exc:
                raise ProviderError(
                    f"{self.name}: unable to read source image: {exc}") from exc
            import base64 as _b64
            mime = str(source_image.get("mime_type") or "image/png")
            parts.insert(0, {"inlineData": {
                "mimeType": mime,
                "data": _b64.b64encode(raw).decode("ascii")}})
        mods = ["TEXT", "IMAGE"] if "2.5" in model else ["IMAGE"]
        body = {"contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"responseModalities": mods}}
        url = f"{self._native_base()}/models/{model}:generateContent"

        def once(cred):
            secret = self.image_pool.get_secret_for(cred)
            headers = {"content-type": "application/json",
                       "x-goog-api-key": secret}
            data = self._post_with_headers(url, body, cred, headers)
            self._done(cred)
            return data

        data = self._run(once, pool=self.image_pool, single_attempt=True)
        uri = self._inline_image(data)
        if not uri:
            err = ProviderError(
                f"{self.name}: image generation returned no image data")
            err.retryable = False
            raise err
        return uri

class AstraGatewayGroq(_GatewayCompatibleConnection):
    """Astra AI Gateway / Groq connection (independent of GroqAdapter)."""
    name = "astra-gw-groq"
    base_url = "https://api.groq.com/openai/v1"
    models_env = "GW_GROQ_MODELS"
    api_keys_env = "GW_GROQ_API_KEYS"
    base_url_env = "GW_GROQ_BASE_URL"
    capabilities = ["chat", "stream", "tools", "json"]


class AstraGatewayCloudflare(_GatewayCompatibleConnection):
    """Astra AI Gateway / Cloudflare connection (independent of
    CloudflareAdapter). Implements its own per-account round-robin over the
    independent GW_CLOUDFLARE_ACCOUNT_IDS list.
    """
    name = "astra-gw-cloudflare"
    #: Canonical, non-``GW_``-prefixed image-model list, owned by
    #: ImageRouter (astra/ai/image_router.py) — shared with the
    #: Provider/ModelRegistry system's own catalog seed (astra/ai/models.py),
    #: since both hold the identical documented FREE model ids.
    image_models_env = "CLOUDFLARE_IMAGE_MODELS"
    base_url = "https://api.cloudflare.com/client/v4"
    models_env = "GW_CLOUDFLARE_MODELS"
    api_keys_env = "GW_CLOUDFLARE_API_KEYS"
    base_url_env = "GW_CLOUDFLARE_BASE_URL"
    account_ids_env = "GW_CLOUDFLARE_ACCOUNT_IDS"
    image_api_keys_env = "IMAGE_CLOUDFLARE_API_KEY"
    image_base_url_env = "IMAGE_CLOUDFLARE_BASE_URL"
    #: Dedicated account id(s) for image generation. ISOLATED from the chat
    #: `account_ids_env` (`GW_CLOUDFLARE_ACCOUNT_IDS`) -- when unset, image
    #: generation is SKIPPED, it never borrows the chat account id (see
    #: `_image_accounts` in `__init__` / `image_credentials_configured()`).
    image_account_ids_env = "IMAGE_CLOUDFLARE_ACCOUNT_ID"
    capabilities = ["chat", "stream", "tools", "json"]

    def __init__(self, config=None, events=None, pool=None):
        super().__init__(config, events, pool)
        accounts = (config.getlist(self.account_ids_env) if config else []) or []
        self._accounts = accounts or []
        image_accounts = (config.getlist(self.image_account_ids_env)
                          if config else []) or []
        # Dedicated image account id(s) ONLY -- no fallback to the chat
        # `GW_CLOUDFLARE_ACCOUNT_IDS` pool. Missing IMAGE_CLOUDFLARE_ACCOUNT_ID
        # means Cloudflare image generation is skipped (see
        # `image_credentials_configured`), not run against the chat account.
        self._image_accounts = list(image_accounts)
        self._aidx = 0
        self._aidx_lock = threading.Lock()
        self._image_aidx = 0
        self._image_aidx_lock = threading.Lock()

    def image_credentials_configured(self) -> bool:
        """Cloudflare image eligibility requires a usable dedicated key AND
        a dedicated account id — each is ONLY the ``IMAGE_CLOUDFLARE_*``
        value; neither falls back to the chat GW_* pool/account ids (see
        `image_pool` / `_image_accounts`). Missing either means Cloudflare
        image generation is skipped, not run against chat credentials."""
        return bool(self.image_pool) and bool(self._image_accounts)

    def _api_base(self) -> str:
        # Per-request account pick; never mutates self.base_url (shared by
        # concurrent request threads).
        if not self._accounts:
            raise ProviderError(
                f"{self.name}: no account ids configured ({self.account_ids_env})")
        with self._aidx_lock:
            acc = self._accounts[self._aidx % len(self._accounts)]
            self._aidx += 1
        return f"{self.base_url}/accounts/{acc}/ai/v1"

    def _image_api_base(self) -> str:
        # Bare base only (no account/path suffix) -- `generate_image` appends
        # `/accounts/<id>/ai/run/<model>` itself. The chat `_api_base()`
        # embeds a *chat* account id + `/ai/v1`, which is the wrong shape for
        # the image path, so this is NOT `self._api_base()`. Falls back to
        # `type(self).base_url` (this class's hardcoded documented default),
        # NEVER to `self.base_url`, which may have been overridden by the
        # chat-only GW_CLOUDFLARE_BASE_URL.
        return (self.image_base_url or type(self).base_url or "").rstrip("/")

    def _image_account(self) -> str:
        """Per-request account pick from the image account pool
        (`IMAGE_CLOUDFLARE_ACCOUNT_ID` when configured, else the chat
        `GW_CLOUDFLARE_ACCOUNT_IDS` fallback -- see `_image_accounts`)."""
        if not self._image_accounts:
            raise ProviderError(
                f"{self.name}: no account ids configured "
                f"({self.image_account_ids_env})")
        with self._image_aidx_lock:
            acc = self._image_accounts[self._image_aidx % len(self._image_accounts)]
            self._image_aidx += 1
        return acc


# ── additional OpenAI-compatible Gateway connections ────────────────────────
# OpenRouter, Mistral, Cerebras, SambaNova, Cohere and Z.AI all expose the
# OpenAI `{model, messages, max_tokens}` → `choices[].message.content` dialect
# over HTTPS with `Authorization: Bearer <key>` (Cohere through its official
# OpenAI-compatibility endpoint, Z.AI through its v4 endpoint), so each is a
# thin declaration over the shared base above: official base URL + its own
# GW_* env names + the capabilities that provider actually supports. Auth,
# timeouts, bounded retries, error/rate-limit classification, credential
# rotation, health and usage capture are all inherited unchanged.

    # ── image generation/editing/inpainting (Workers AI /ai/run/<model>)
    def generate_image(self, prompt: str, model: str | None = None,
                       size: str = "1024x1024", n: int = 1, *,
                       source_image: dict | None = None,
                       mask_image: dict | None = None) -> str:
        """Run a verified Cloudflare image model with optional img2img/mask."""
        model = model or self._default_image_model()
        if not model:
            raise ProviderError(f"{self.name}: no image model configured")
        if not self._image_accounts:
            raise ProviderError(
                f"{self.name}: no account ids configured "
                f"({self.image_account_ids_env})")
        from astra.ai.image_models import (
            image_spec, IMAGE_EDITING, IMAGE_INPAINTING)
        spec = image_spec("cloudflare", model)
        allowed = set(spec.params) if spec else {"prompt"}
        body = {"prompt": prompt}
        if "width" in allowed and "height" in allowed:
            try:
                w, h = (int(x) for x in str(size).lower().split("x"))
            except (ValueError, AttributeError):
                w, h = 1024, 1024
            body["width"] = max(256, min(w, 2048))
            body["height"] = max(256, min(h, 2048))

        def _read_bytes(source: dict, label: str) -> bytes:
            path = str(source.get("storage_path") or "")
            if not path or not os.path.isfile(path):
                raise ProviderError(f"{self.name}: {label} is unavailable")
            with open(path, "rb") as fh:
                raw = fh.read()
            if not raw:
                raise ProviderError(f"{self.name}: {label} is empty")
            return raw

        if source_image is not None:
            if not spec or IMAGE_EDITING not in spec.capabilities:
                raise ProviderError(
                    f"{self.name}: model {model} does not support image editing")
            import base64 as _b64
            body["image_b64"] = _b64.b64encode(
                _read_bytes(source_image, "source image")).decode("ascii")
            if "strength" in allowed:
                body["strength"] = 0.75

        if mask_image is not None:
            if not spec or IMAGE_INPAINTING not in spec.capabilities:
                raise ProviderError(
                    f"{self.name}: model {model} does not support inpainting")
            raw_mask = _read_bytes(mask_image, "mask image")
            body["mask"] = list(raw_mask)

        def once(cred):
            acc = self._image_account()
            url = f"{self._image_api_base()}/accounts/{acc}/ai/run/{model}"
            raw = self._post_raw(url, body, cred)
            self._done(cred)
            return raw

        raw = self._run(once, pool=self.image_pool, single_attempt=True)
        uri = ""
        if raw[:1] == b"{":
            try:
                uri = image_result_to_data_uri(
                    json.loads(raw.decode("utf-8", "replace")))
            except ValueError:
                uri = ""
        if not uri and raw[:1] != b"{":
            import base64 as _b64
            mime = ("image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n"
                    else "image/jpeg" if raw[:3] == b"\xff\xd8\xff"
                    else "image/png")
            uri = f"data:{mime};base64," + _b64.b64encode(raw).decode("ascii")
        if not uri:
            err = ProviderError(
                f"{self.name}: image generation returned no image data")
            err.retryable = False
            raise err
        return uri

class AstraGatewayOpenRouter(_GatewayCompatibleConnection):
    """Astra AI Gateway / OpenRouter connection (independent of
    OpenRouterAdapter)."""
    name = "astra-gw-openrouter"
    #: Canonical, non-``GW_``-prefixed image-model list — shared with the
    #: Provider/ModelRegistry system's own catalog seed (astra/ai/models.py).
    #: Left empty by default: OpenRouter's live discovery
    #: (`_discover_image_models`) is authoritative for its FREE image ids.
    #: Owned by ImageRouter (astra/ai/image_router.py), like the other two
    #: canonical image-model lists.
    image_models_env = "OPENROUTER_IMAGE_MODELS"
    base_url = "https://openrouter.ai/api/v1"
    models_env = "GW_OPENROUTER_MODELS"
    api_keys_env = "GW_OPENROUTER_API_KEYS"
    base_url_env = "GW_OPENROUTER_BASE_URL"
    image_api_keys_env = "IMAGE_OPENROUTER_API_KEY"
    image_base_url_env = "IMAGE_OPENROUTER_BASE_URL"
    # Optional attribution headers OpenRouter documents for API clients.
    extra_headers = {
        "HTTP-Referer": "https://github.com/mainnetwallet/Astra-AI-Agent",
        "X-Title": "Astra AI Agent",
    }
    capabilities = ["chat", "stream", "tools", "json", "vision"]

    # ── image generation: official discovery + Image API ──────────────────
    #: OpenRouter's documented image-model discovery endpoint. Its returned
    #: output modalities are authoritative for the exact model ids it lists.
    IMAGE_MODELS_URL = "https://openrouter.ai/api/v1/images/models"

    def __init__(self, config=None, events=None, pool=None):
        super().__init__(config, events, pool)
        self._discovered_image_models = None
        self._discovered_at = 0.0

    def _discover_image_models(self, *, discover: bool = True) -> list:
        """LIVE discovery results only (empty when unreachable). Cached for
        10 minutes; a failed discovery never invents models."""
        now = time.time()
        fresh = (self._discovered_image_models is not None
                 and now - self._discovered_at < 600)
        if discover and not fresh:
            found = []
            try:
                req = urllib.request.Request(
                    self.IMAGE_MODELS_URL,
                    headers={"User-Agent": "astra-ai-agent"})
                with urllib.request.urlopen(req, timeout=GW_DEFAULT_TIMEOUT) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "replace"))
                for item in (payload.get("data") or []):
                    mid = (item or {}).get("id")
                    if not mid:
                        continue
                    arch = item.get("architecture") or {}
                    outs = arch.get("output_modalities") or []
                    if not outs or "image" not in outs:
                        continue
                    # OpenRouter marks its free variants with a ":free"
                    # suffix; anything else is paid and must not enter the
                    # FREE image pool.
                    if not mid.endswith(":free"):
                        continue
                    found.append(mid)
            except Exception:
                found = []
            # Cache failures too: an unreachable discovery API must not add a
            # network timeout to every image request for the next 10 minutes.
            self._discovered_image_models = found
            self._discovered_at = now
        return list(self._discovered_image_models or [])

    def live_image_models(self, *, discover: bool = True) -> list:
        """Ids the provider's OWN API reported as image-output models.

        These are authoritative for image capability even when they are not
        in the static registry (spec section 11) -- but only for a provider
        whose image API this repo can actually call.
        """
        return self._discover_image_models(discover=discover)

    def list_image_models(self, *, discover: bool = True) -> list:
        """Configured image models plus LIVE discovery results.

        OpenRouter's chat model list says nothing about image capability, so
        the dedicated discovery API is consulted instead. A failed/blocked
        discovery never invents models: it falls back to the configured list.
        """
        ids = list(self.image_models)
        for mid in self._discover_image_models(discover=discover):
            if mid not in ids:
                ids.append(mid)
        return ids

    def generate_image(self, prompt: str, model: str | None = None,
                       size: str = "1024x1024", n: int = 1) -> str:
        """Generate an image via OpenRouter's dedicated Images API.

        ``POST {base}/images`` -- the current documented endpoint (NOT the
        OpenAI-style ``/images/generations``). Only documented Image API
        fields are sent (``model``, ``prompt``, ``n``, ``size``); the OpenAI
        ``response_format`` field is deliberately omitted. The response's
        ``data[].b64_json`` is normalized into ``data:<mime>;base64,...``,
        and a 400/401/402/403/404/413/429/500/502/524/529 or network error
        propagates as a ProviderError so the Gateway's global serial
        fallback advances to the next eligible image model.
        """
        model = model or self._default_image_model()
        if not model:
            raise ProviderError(f"{self.name}: no image model configured")
        body = {"model": model, "prompt": prompt, "n": max(1, int(n or 1))}
        if size:
            body["size"] = size

        def once(cred):
            data = self._post(f"{self._image_api_base()}/images", body, cred)
            self._done(cred)
            return data

        data = self._run(once, pool=self.image_pool, single_attempt=True)
        uri = image_result_to_data_uri(data)
        if not uri:
            err = ProviderError(
                f"{self.name}: image generation returned no image data")
            err.retryable = False
            raise err
        return uri


class AstraGatewayMistral(_GatewayCompatibleConnection):
    """Astra AI Gateway / Mistral connection (independent of MistralAdapter)."""
    name = "astra-gw-mistral"
    base_url = "https://api.mistral.ai/v1"
    models_env = "GW_MISTRAL_MODELS"
    api_keys_env = "GW_MISTRAL_API_KEYS"
    base_url_env = "GW_MISTRAL_BASE_URL"
    capabilities = ["chat", "stream", "tools", "json", "coding"]


class AstraGatewayCerebras(_GatewayCompatibleConnection):
    """Astra AI Gateway / Cerebras connection (independent of CerebrasAdapter)."""
    name = "astra-gw-cerebras"
    base_url = "https://api.cerebras.ai/v1"
    models_env = "GW_CEREBRAS_MODELS"
    api_keys_env = "GW_CEREBRAS_API_KEYS"
    base_url_env = "GW_CEREBRAS_BASE_URL"
    capabilities = ["chat", "stream", "tools", "json"]


class AstraGatewaySambaNova(_GatewayCompatibleConnection):
    """Astra AI Gateway / SambaNova connection (independent of
    SambaNovaAdapter). Accepts both the canonical `GW_SAMBANOVA_*` names and
    the provider's shorter `GW_SAMBA_*` prefix."""
    name = "astra-gw-sambanova"
    base_url = "https://api.sambanova.ai/v1"
    models_env = "GW_SAMBANOVA_MODELS"
    api_keys_env = "GW_SAMBANOVA_API_KEYS"
    base_url_env = "GW_SAMBANOVA_BASE_URL"
    env_aliases = {
        "GW_SAMBANOVA_API_KEYS": ("GW_SAMBA_API_KEYS",),
        "GW_SAMBANOVA_MODELS": ("GW_SAMBA_MODELS",),
        "GW_SAMBANOVA_BASE_URL": ("GW_SAMBA_BASE_URL",),
    }
    capabilities = ["chat", "stream", "tools", "json"]


class AstraGatewayCohere(_GatewayCompatibleConnection):
    """Astra AI Gateway / Cohere connection (independent of CohereAdapter),
    using Cohere's official OpenAI-compatibility endpoint."""
    name = "astra-gw-cohere"
    base_url = "https://api.cohere.ai/compatibility/v1"
    models_env = "GW_COHERE_MODELS"
    api_keys_env = "GW_COHERE_API_KEYS"
    base_url_env = "GW_COHERE_BASE_URL"
    capabilities = ["chat", "stream", "tools", "json", "vision", "translation"]


class AstraGatewayZAI(_GatewayCompatibleConnection):
    """Astra AI Gateway / Z.AI (GLM) connection (independent of ZAIAdapter)."""
    name = "astra-gw-zai"
    image_models_env = "ZAI_IMAGE_MODELS"
    base_url = "https://api.z.ai/api/paas/v4"
    models_env = "GW_ZAI_MODELS"
    api_keys_env = "GW_ZAI_API_KEYS"
    base_url_env = "GW_ZAI_BASE_URL"
    capabilities = ["chat", "stream", "tools", "json", "vision"]


# ── Gateway Bedrock connection (independent of BedrockAdapter) ──────────────
def _gw_hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _gw_sign_v4(access_key: str, secret_key: str, region: str, service: str,
                method: str, url: str, content_type: str, payload: bytes) -> dict:
    """Gateway-local AWS SigV4 signer — duplicated here (not imported from
    astra.ai.adapters.bedrock) so the Gateway has zero dependency on the
    Provider Bedrock adapter module."""
    from urllib.parse import urlsplit, quote
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    parts = urlsplit(url)
    host = parts.netloc
    canonical_uri = quote(parts.path, safe="/-_.~") or "/"
    canonical_querystring = parts.query or ""

    signed_headers = "content-type;host;x-amz-date"
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{host}\n"
        f"x-amz-date:{amz_date}\n")
    payload_hash = hashlib.sha256(payload).hexdigest()
    canonical_request = "\n".join([
        method, canonical_uri, canonical_querystring, canonical_headers,
        signed_headers, payload_hash])
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    k_date = _gw_hmac(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _gw_hmac(k_date, region)
    k_service = _gw_hmac(k_region, service)
    k_signing = _gw_hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    auth = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")
    return {"Content-Type": content_type, "Host": host,
            "X-Amz-Date": amz_date, "Authorization": auth}


class _GatewayBedrockCredentialPool(CredentialPool):
    """Parses `access_key:secret_key` pairs from GW_BEDROCK_CREDENTIALS.
    A Gateway-local pool class — not astra.ai.adapters.bedrock.BedrockCredentialPool."""

    @classmethod
    def from_env(cls, config, env_name: str, provider: str | None = None):
        pairs: list[str] = []
        text = getattr(config, "get", lambda _k, d="": d)(env_name, "") or ""
        for line in text.replace(",", "\n").splitlines():
            line = line.strip()
            if line and ":" in line:
                pairs.append(line)
        pool = cls(provider or "astra-gw-bedrock", [])
        for entry in pairs:
            ak, _, sk = entry.partition(":")
            if ak and sk:
                pool.add(entry)
        return pool

    def add(self, secret: str):
        ak, _, sk = secret.partition(":")
        return super().add(f"{ak}:{sk}")


class AstraGatewayBedrock:
    """Astra AI Gateway / Bedrock connection (independent of BedrockAdapter).

    Own two auth modes — GW_BEDROCK_API_KEYS (bearer) or GW_BEDROCK_CREDENTIALS
    (SigV4 access_key:secret_key pairs) — own region (GW_BEDROCK_REGION, no
    fallback to the shared AWS_REGION/AWS_ACCESS_KEY_ID env vars the Provider
    Bedrock adapter reads) and own model list/base URL.
    """
    name = "astra-gw-bedrock"
    capabilities = ["chat", "stream", "tools", "json", "vision"]

    models_env = "GW_BEDROCK_MODELS"
    base_url_env = "GW_BEDROCK_BASE_URL"
    api_keys_env = "GW_BEDROCK_API_KEYS"
    credentials_env = "GW_BEDROCK_CREDENTIALS"
    region_env = "GW_BEDROCK_REGION"
    default_region = "us-east-1"

    def __init__(self, config=None, events=None, pool=None):
        self.config = config
        self.events = events
        if pool is not None:
            self.pool = pool
            self.auth_mode = "sigv4" if isinstance(pool, _GatewayBedrockCredentialPool) else "bearer"
        else:
            api_keys = config.getlist(self.api_keys_env, default=[]) if config else []
            if api_keys:
                self.auth_mode = "bearer"
                self.pool = CredentialPool.from_env(config, self.api_keys_env, self.name)
            else:
                self.auth_mode = "sigv4"
                self.pool = _GatewayBedrockCredentialPool.from_env(
                    config, self.credentials_env, self.name)
        self.region = (config.get(self.region_env) if config else None) or self.default_region
        raw = (config.get(self.base_url_env) if config else None) \
            or f"https://bedrock-runtime.{self.region}.amazonaws.com"
        self.base_url = raw.rstrip("/")
        self.models = config.getlist(self.models_env, default=[]) if config else []
        # Converse reports {inputTokens, outputTokens, totalTokens}; preserved
        # verbatim (secret-free) for callers that want usage metadata.
        self._last_usage: dict = {}

    def last_usage(self) -> dict:
        return dict(self._last_usage or {})

    # -- signing --------------------------------------------------------------
    def _sign(self, cred, url: str, payload: bytes) -> dict:
        if self.auth_mode == "bearer":
            secret = self.pool.get_secret_for(cred)
            return {"Content-Type": "application/json",
                   "Authorization": f"Bearer {secret}"}
        ak, _, sk = self.pool.get_secret_for(cred).partition(":")
        return _gw_sign_v4(ak, sk, self.region, "bedrock", "POST", url,
                           "application/json", payload)

    def _post(self, url: str, body: dict, cred) -> dict:
        payload = json.dumps(body).encode("utf-8")
        headers = self._sign(cred, url, payload)
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            self.pool.report_failure(cred, reason=f"{self.name} http {e.code}",
                                     auth_failure=e.code in (401, 403),
                                     rate_limited=e.code == 429,
                                     cooldown_s=(45 if e.code == 429 else 30))
            code = e.code
            _close_http_error(e)
            if code in (401, 403):
                raise ProviderError(f"{self.name} authentication/authorization failed")
            if code == 429:
                raise ProviderError(f"{self.name} rate limit reached")
            raise ProviderError(f"{self.name} http {code}")
        except urllib.error.URLError as e:
            raise ProviderError(f"{self.name} network: {getattr(e, 'reason', e)}") from e
        self.pool.report_success(cred)
        return data

    # -- Converse shapes ------------------------------------------------------
    def _converse_body(self, messages, model, max_tokens) -> dict:
        system = "\n".join(m.get("content", "") for m in messages
                           if m.get("role") == "system")
        convo = []
        for m in messages:
            if m.get("role") == "system":
                continue
            role = "assistant" if m.get("role") == "assistant" else "user"
            content = m.get("content", "")
            if isinstance(content, list):
                blocks = [{"text": (b.get("text") if isinstance(b, dict) else str(b))}
                          for b in content]
            else:
                blocks = [{"text": str(content)}]
            convo.append({"role": role, "content": blocks})
        body = {"modelId": model, "messages": convo}
        if max_tokens is not None:
            body["inferenceConfig"] = {"maxTokens": max_tokens}
        if system:
            body["system"] = [{"text": system}]
        return body

    def chat(self, messages, model=None, max_tokens=None) -> str:
        model = model or (self.models[0] if self.models else "")
        if not model:
            raise ProviderError(f"{self.name}: no model configured")
        cred = self.pool.pick()
        if cred is None:
            raise ProviderError(f"{self.name}: no healthy credential configured")
        body = self._converse_body(messages, model, max_tokens)
        data = self._post(f"{self.base_url}/model/{model}/converse", body, cred)
        self._last_usage = data.get("usage", {}) or {}
        blocks = data.get("output", {}).get("message", {}).get("content", [])
        return "".join(b.get("text", "") for b in blocks).strip() or "(no reply)"

    def stream(self, messages, model=None, max_tokens=None):
        model = model or (self.models[0] if self.models else "")
        if not model:
            raise ProviderError(f"{self.name}: no model configured")
        cred = self.pool.pick()
        if cred is None:
            raise ProviderError(f"{self.name}: no healthy credential configured")
        if self.events:
            self.events.emit("ai.started", agent="gateway", provider=self.name, model=model)
        body = self._converse_body(messages, model, max_tokens)
        payload = json.dumps(body).encode("utf-8")
        url = f"{self.base_url}/model/{model}/converse-stream"
        req = urllib.request.Request(url, data=payload,
                                     headers=self._sign(cred, url, payload))
        import io
        full = ""
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                for line in io.TextIOWrapper(resp, encoding="utf-8", errors="replace"):
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[len("data:"):].strip())
                    except ValueError:
                        continue
                    if event.get("contentBlockDelta", {}).get("delta", {}).get("text"):
                        text = event["contentBlockDelta"]["delta"]["text"]
                        full += text
                        yield text
        except urllib.error.HTTPError as e:
            code = e.code
            _close_http_error(e)
            raise ProviderError(f"{self.name} stream http {code}") from e
        self.pool.report_success(cred)
        if self.events:
            self.events.emit("ai.completed", agent="gateway", provider=self.name,
                             length=len(full))

    def health_check(self) -> bool:
        return self.pool.healthy_count > 0

    def credential_summary(self) -> dict:
        return self.pool.summary()

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


# Canonical fallback order. The original four connections keep their exact
# relative order (Gemini → Groq → Cloudflare → Bedrock) so existing routing/
# scoring behaviour is unchanged; the six additional connections follow in a
# fixed order and are simply skipped when unconfigured.
GATEWAY_CONNECTIONS = (
    AstraGatewayGemini,
    AstraGatewayGroq,
    AstraGatewayCloudflare,
    AstraGatewayBedrock,
    AstraGatewayOpenRouter,
    AstraGatewayMistral,
    AstraGatewayCerebras,
    AstraGatewaySambaNova,
    AstraGatewayCohere,
    AstraGatewayZAI,
)


def _build_connection(cls, config=None):
    """Build one gateway connection iff EITHER its chat credentials OR its
    dedicated image credentials are configured.

    Returns None when the connection's GW_*_API_KEYS / GW_*_CREDENTIALS AND
    its dedicated IMAGE_* API key (when the class declares one) are all
    unset/blank, so an unconfigured connection simply drops out of the
    fallback chain — same convention as every provider adapter.

    A connection built ONLY from its dedicated IMAGE_* credential (no
    GW_*_API_KEYS at all) is still excluded from ordinary chat routing,
    since `_connection_usable()` checks the chat `pool`, which stays empty —
    it only ever becomes eligible for image generation, via
    `image_credentials_configured()` / `eligible_image_generation_targets()`.
    This is what lets ImageRouter use IMAGE_* credentials completely
    independently of whether the matching GW_* chat credentials exist.
    """
    if config is None:
        return None
    # gateway connections are independent of provider config; only GW_* counts
    aliases = getattr(cls, "env_aliases", {}) or {}

    def _vals(env_name):
        if not env_name:
            return []
        return [k for name in (env_name,) + tuple(aliases.get(env_name, ()))
                for k in config.getlist(name, default=[])]

    keys = _vals(cls.api_keys_env)
    if not keys:
        keys = _vals(getattr(cls, "credentials_env", None))
    image_keys = _vals(getattr(cls, "image_api_keys_env", None))
    if not keys and not image_keys:
        return None
    try:
        return cls(config=config)
    except Exception:
        return None

    # ── image generation (InvokeModel) ────────────────────────────────────
    image_models_env = "BEDROCK_IMAGE_MODELS"

    def list_image_models(self, *, discover: bool = True) -> list:
        return list(getattr(self, "image_models", []) or [])

    def generate_image(self, prompt: str, model: str | None = None,
                       size: str = "1024x1024", n: int = 1) -> str:
        """Generate an image with a documented Bedrock image model.

        The model is validated against astra.ai.image_models first: a
        Claude/Nova text model must never be handed a Titan/Stability/
        Nova-Canvas image request body (the Gateway's Converse path stays
        strictly separate).
        """
        from astra.ai.image_models import is_image_model
        model = model or (self.image_models[0] if self.image_models else "")
        if not model:
            raise ProviderError(f"{self.name}: no image model configured")
        if not is_image_model("bedrock", model):
            err = ProviderError(
                f"{self.name}: {model} is not an image-generation model")
            err.retryable = False
            raise err
        try:
            w, h = (int(x) for x in str(size).split("x"))
        except (ValueError, AttributeError):
            w, h = 1024, 1024
        low = model.lower()
        if "nova-canvas" in low:
            body = {"taskType": "TEXT_IMAGE",
                    "textToImageParams": {"text": prompt},
                    "imageGenerationConfig": {"numberOfImages": 1, "width": w,
                                              "height": h, "quality": "standard",
                                              "cfgScale": 7.0}}
        elif "titan-image" in low:
            body = {"taskType": "TEXT_IMAGE",
                    "textToImageParams": {"text": prompt},
                    "imageGenerationConfig": {"numberOfImages": min(n, 1),
                                              "width": w, "height": h}}
        else:
            body = {"text_prompts": [{"text": prompt}], "cfg_scale": 7,
                    "steps": 30, "width": w, "height": h}
        url = f"{self.base_url}/model/{model}/invoke"

        def once(cred):
            data = self._post(url, body, cred)
            self._done(cred)
            return data

        data = self._run(once, single_attempt=True)
        uri = image_result_to_data_uri(data)
        if not uri:
            err = ProviderError(
                f"{self.name}: image generation returned no image data")
            err.retryable = False
            raise err
        return uri

class AstraAIGateway:
    """Multi-provider, multi-model intelligent-routing gateway.

    Astra AI Gateway has ten connections (Gemini, Groq, Cloudflare, Bedrock,
    OpenRouter, Mistral, Cerebras, SambaNova, Cohere, Z.AI), each of which may
    expose multiple models (§1) and each of which is optional — an
    unconfigured connection is simply absent. For every request the Gateway:

        1. classifies the request (simple/general/reasoning/coding/
           long_context/structured_output/tool_use/vision) — locally,
           deterministically, without spending a model call on it (§10);
        2. filters the full provider+model catalog down to targets that are
           actually suitable (capability/context) and not in cooldown (§2,
           §6, §8);
        3. prefers the last successful target when it's still suitable and
           healthy, otherwise ranks the rest by capability fit, health,
           latency and configured priority (§3, §5, §9, §15);
        4. attempts targets in that order — a model-level failure only
           deprioritizes that (provider, model) pair, never the whole
           connection (§6/§7) — until one succeeds or every suitable target
           has been tried (bounded; no infinite retries).

    A caller may still request a *specific* model explicitly (`model=...`);
    that bypasses the intelligent selection entirely and falls back only
    across connections that actually serve it, in configured order (§17) —
    exactly the original, simpler fallback behavior this class always had.

    See astra/ai/gateway_routing.py for the actual classification/
    scoring/health/persistence logic — this class only owns HTTP execution
    and the attempt loop.

    IMAGE GENERATION is the deliberate exception to steps 3/4 above: it
    uses a SIMPLE SERIAL FALLBACK over the FREE image-model pool ImageRouter
    owns, in a deterministic priority order (astra.ai.image_models), with NO
    proactive health check and no cooldown gating -- the actual generation
    request is the availability signal, and a failure is remembered only for
    that one request. The Gateway owns no image-model configuration of its
    own; it only classifies and hands off. See `image_targets()` /
    `generate_image()`.
    """

    def __init__(self, connections: list | None = None, config=None,
                store=None, events=None):
        self.name = "astra_ai_gateway"
        self.config = config
        self.events = events
        self.connections = []
        self.last_connection = ""     # name of the most recent successful call
        self.last_model = ""
        self.last_attempts = 0
        self.last_category = ""       # classification of the most recent request
        # Dedicated image-generation execution owner (see
        # astra/ai/image_router.py). The Gateway itself performs NO provider
        # image-API HTTP call -- it only classifies/hands off. Imported
        # lazily here (rather than at module scope) to avoid a circular
        # import, since ImageRouter only needs the Gateway instance at call
        # time.
        from astra.ai.image_router import ImageRouter
        self.image_router = ImageRouter(self)
        if connections is not None:
            self.connections = list(connections)
        elif config is not None:
            for cls in GATEWAY_CONNECTIONS:
                conn = _build_connection(cls, config)
                if conn is not None:
                    self.connections.append(conn)
        from astra.ai.gateway_routing import (GatewayRoutingState,
                                              build_gateway_catalog)
        self.routing_state = GatewayRoutingState(store)
        # Manual-health-check dedup with the Provider system (see
        # astra/ai/shared_health.py). `AstraRouter` adopts THIS instance
        # when it wires a Gateway in (see its __init__), so the two
        # converge on one coordinator when used together; this default
        # keeps a standalone Gateway (no Router attached) working too.
        self.shared_health = SharedHealthCoordinator(store=store)
        self._catalog = build_gateway_catalog(self.connections)
        # §2-§5, §18: task-level execution recovery for the EXISTING
        # Provider system's own catalog — a completely separate namespace
        # from `self.routing_state` above (which only ever tracks this
        # Gateway's own GW_* connections). See gateway_recovery.py.
        from astra.ai.gateway_recovery import GatewayExecutionRecovery
        self.execution_recovery = GatewayExecutionRecovery(store=store,
                                                            events=events)
        # §6-§12: Gateway-OWNED result validation + bounded correction loop,
        # distinct from execution_recovery above (which only ever decides
        # WHO to execute against). See gateway_supervision.py.
        from astra.ai.gateway_supervision import GatewayResultSupervision
        self.result_supervision = GatewayResultSupervision(events=events)
        # §1-§8: Task Completion Contract + evidence/semantic verification
        # + correction, distinct from result_supervision above (which only
        # ever checks deterministic response *shape* — non-empty/JSON/
        # required fields). See gateway_task_completion.py.
        from astra.ai.gateway_task_completion import GatewayTaskCompletionSupervisor
        self.task_completion = GatewayTaskCompletionSupervisor(events=events)

    def attach_events(self, events) -> None:
        self.events = events

    # -- capability -----------------------------------------------------------
    @property
    def models(self) -> list[str]:
        out = []
        for c in self.connections:
            out.extend(c.models or [])
        return out

    def is_usable(self) -> bool:
        """At least one connection has healthy credentials."""
        for c in self.connections:
            pool = getattr(c, "pool", None)
            if pool is not None and bool(pool):
                return True
            try:
                if c.health_check():
                    return True
            except Exception:
                continue
        return bool(self.connections)

    # -- explicit-model fallback (§17: user-requested model is respected) ----
    def _try_connections(self, fn, *, model, messages, max_tokens,
                         op="", trace=""):
        """Run `fn` over the connections in order; first success wins.

        Used only when the caller names a specific model. Tracks which
        connection/model served the request (`last_connection`,
        `last_model`) and how many connections were attempted before
        success (`last_attempts`) so callers can report the real serving
        path.
        """
        last_error = ""
        attempts = 0
        self._emit("astra_gateway.request", category="explicit_model",
                   model=model or "", op=op, trace=trace,
                   input=_gw_log_input(messages))
        for conn in self.connections:
            attempts += 1
            self.last_attempts = attempts
            call_model = model
            if call_model and not conn.models:
                # no models configured for this connection at all —
                # treat it as disabled, never attempt a call.
                continue
            if call_model and call_model not in conn.models:
                # this connection doesn't serve the requested model —
                # skip it rather than fail it (fallback intent is a
                # different model on the next service).
                continue
            short = _short_provider(conn)
            used_model = call_model or (conn.models[0] if conn.models else "")
            start = time.perf_counter()
            try:
                result = fn(conn, call_model)
            except (ProviderError, TimeoutError) as e:
                last_error = getattr(e, "message", None) or str(e)
                self._emit("astra_gateway.error", provider=short,
                           model=used_model, reason=last_error, op=op,
                           trace=trace, terminal=False, attempt=attempts)
                continue
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                self._emit("astra_gateway.error", provider=short,
                           model=used_model, reason=last_error, op=op,
                           trace=trace, terminal=False, attempt=attempts)
                continue
            latency_ms = (time.perf_counter() - start) * 1000.0
            self.last_connection = conn.name
            self.last_model = used_model
            self._emit("astra_gateway.success", provider=short,
                       model=used_model, latency_ms=round(latency_ms, 1),
                       op=op, trace=trace, terminal=True,
                       output=_gw_log_output(result))
            return result
        self.last_connection = ""
        self.last_model = ""
        self._emit("astra_gateway.error", provider="", model=model or "",
                   reason=last_error or "all services failed", op=op,
                   trace=trace, terminal=True, attempts=attempts)
        raise ProviderError(
            f"Astra AI Gateway: all configured services failed —— {last_error}")

    # -- intelligent multi-provider/multi-model selection (§2/§15) -----------
    def _classification_inputs(self, messages) -> tuple[str, int, bool]:
        """Classification text comes ONLY from user-role content — a
        system/instruction message (e.g. GATEWAY_UNDERSTANDING_SYSTEM_PROMPT
        below, or any caller's own system prompt) is Gateway/Provider
        control text, not the user's actual request, and must never leak
        into category detection (a system prompt that merely *mentions*
        "structured"/"code" must not force a structured_output/coding hard
        capability filter). `context_tokens`, by contrast, sums every
        message — the full context genuinely has to fit the model."""
        texts, vision, chars = [], False, 0
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            content = m.get("content", "")
            is_user = m.get("role", "user") == "user"
            if isinstance(content, str):
                chars += len(content)
                if is_user:
                    texts.append(content)
            elif isinstance(content, list):
                # multi-part content (e.g. text + image blocks) — a list
                # content shape is the one clear, non-guessy vision signal
                # available at this layer without inventing capabilities.
                vision = True
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        chars += len(block["text"])
                        if is_user:
                            texts.append(block["text"])
        text = " ".join(texts)
        # No tokenizer is bundled, so this is an ESTIMATE, not an exact token
        # count (see astra.ai.context_budget); it feeds the model-selection
        # context gate, which is itself a soft preference now that the
        # selected model's real window is used to fit the prompt below.
        from astra.ai.context_budget import estimate_tokens_from_chars
        context_tokens = estimate_tokens_from_chars(chars)
        return text, context_tokens, vision

    def _catalog_model(self, model_id):
        """Look up the live `Model` for a model id from this gateway's own
        catalog, so an explicit-model call can fit the prompt to that model's
        real context window. Returns None when unknown."""
        if not model_id:
            return None
        for _conn, model in self._catalog:
            if getattr(model, "model_id", "") == model_id:
                return model
        return None

    @staticmethod
    def _fit_messages(messages, model, max_tokens=None):
        """Provider-aware context fitting: keep the full prompt when it fits
        the target model's real context window, otherwise drop the oldest
        middle turns (never the system prompt or the current request). See
        astra.ai.context_budget."""
        from astra.ai.context_budget import default_reserve_tokens, fit_messages
        from astra.ai.token_limits import resolve_output_tokens
        ctx = int(getattr(model, "context_window", 0) or 0)
        out = resolve_output_tokens(max_tokens,
                                    provider=getattr(model, "provider", ""),
                                    model_meta=model)
        reserve = out if out else default_reserve_tokens(ctx)
        return fit_messages(messages, context_window=ctx,
                            reserve_tokens=reserve)

    def _select_order(self, messages, max_tokens, category=None):
        from astra.ai.gateway_routing import (REQUEST_CATEGORIES,
                                              classify_gateway_request,
                                              eligible_targets, rank_targets,
                                              prefer_last_successful)
        text, context_tokens, vision = self._classification_inputs(messages)
        if category in REQUEST_CATEGORIES:
            # Caller knows what kind of call this is (e.g. the chat
            # pipeline's own understand/verify calls) — keyword-sniffing the
            # text would mis-classify control prompts that merely mention
            # "vision"/"json"/"table" and hard-filter out every model.
            pass
        else:
            category = classify_gateway_request(
                text, vision=vision, context_tokens=context_tokens)
        self.last_category = category
        targets = eligible_targets(self._catalog, self.routing_state,
                                   category=category,
                                   context_tokens=context_tokens)
        if not targets and context_tokens:
            # The prompt does not fit any candidate's window AS-IS, but
            # astra.ai.context_budget will fit it to whichever model is
            # selected — so a large context must not hard-fail target
            # selection. Retry the gate without the context filter and let
            # provider-aware fitting shrink the prompt to the chosen model.
            targets = eligible_targets(self._catalog, self.routing_state,
                                       category=category, context_tokens=0)
        if not targets and category == "control":
            # No configured model declares JSON support: control calls must
            # still work, so fall back to the ordinary soft "general" ranking.
            category = "general"
            self.last_category = category
            targets = eligible_targets(self._catalog, self.routing_state,
                                       category=category,
                                       context_tokens=context_tokens)
        ranked = rank_targets(targets, category=category)
        if category != "control":
            # "Stick to the last successful target" would pin a control call
            # to whatever model last served ANY request (often a slow one);
            # control calls rank purely on measured latency + health.
            ranked = prefer_last_successful(
                ranked, self.routing_state.last_successful())
        return category, ranked

    def _emit(self, kind: str, **data) -> None:
        if self.events:
            try:
                self.events.emit(kind, agent="gateway", **data)
            except Exception:
                pass

    def chat(self, messages, model=None, max_tokens=None, category=None,
             trace="") -> str:
        """`category` (optional, one of gateway_routing.REQUEST_CATEGORIES)
        overrides the keyword classification of the user-role text; leave it
        unset for ordinary requests. `max_tokens` unset means the provider /
        model decides (no Astra-imposed cap)."""
        op = new_op_id()
        if model:
            target = self._catalog_model(model)
            fitted = (self._fit_messages(messages, target, max_tokens)
                      if target is not None else messages)
            return self._try_connections(
                lambda c, m: c.chat(fitted, model=m, max_tokens=max_tokens),
                model=model, messages=fitted, max_tokens=max_tokens,
                op=op, trace=trace)

        category, ranked = self._select_order(messages, max_tokens, category)
        self._emit("astra_gateway.request", category=category,
                   candidates=len(ranked), op=op, trace=trace,
                   input=_gw_log_input(messages))
        if not ranked:
            self.last_connection = ""
            self.last_model = ""
            self.last_attempts = 0
            self._emit("astra_gateway.error", provider="", model="",
                       reason="no suitable provider+model available",
                       op=op, trace=trace, terminal=True)
            raise ProviderError(
                f"Astra AI Gateway: no suitable provider+model available "
                f"for this request (category={category})")

        last_error = ""
        attempts = 0
        for conn, target_model, health in ranked:
            attempts += 1
            self.last_attempts = attempts
            start = time.perf_counter()
            try:
                result = conn.chat(
                    self._fit_messages(messages, target_model, max_tokens),
                    model=target_model.model_id, max_tokens=max_tokens)
            except (ProviderError, TimeoutError) as e:
                last_error = getattr(e, "message", None) or str(e)
                self.routing_state.record_failure(target_model.provider,
                                                  target_model.model_id)
                self._emit("astra_gateway.error", provider=target_model.provider,
                          model=target_model.model_id, reason=last_error,
                          op=op, trace=trace, terminal=False, attempt=attempts)
                continue
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                self.routing_state.record_failure(target_model.provider,
                                                  target_model.model_id)
                self._emit("astra_gateway.error", provider=target_model.provider,
                          model=target_model.model_id, reason=last_error,
                          op=op, trace=trace, terminal=False, attempt=attempts)
                continue
            latency_ms = (time.perf_counter() - start) * 1000.0
            self.routing_state.record_success(target_model.provider,
                                              target_model.model_id, latency_ms)
            self.last_connection = conn.name
            self.last_model = target_model.model_id
            self._emit("astra_gateway.success", provider=target_model.provider,
                      model=target_model.model_id, latency_ms=round(latency_ms, 1),
                      op=op, trace=trace, terminal=True,
                      output=_gw_log_output(result))
            return result
        self.last_connection = ""
        self.last_model = ""
        self._emit("astra_gateway.error", provider="", model="",
                   reason=last_error or "all suitable targets failed", op=op,
                   trace=trace, terminal=True, attempts=attempts)
        raise ProviderError(
            f"Astra AI Gateway: all suitable targets failed —— {last_error}")

    def stream(self, messages, model=None, max_tokens=None, trace=""):
        op = new_op_id()
        if model:
            # Generator fallback: first connection's stream that starts wins
            # (unchanged explicit-model path — §17).
            self._emit("astra_gateway.request", category="explicit_model",
                       model=model or "", op=op, trace=trace,
                       input=_gw_log_input(messages))
            for conn in self.connections:
                call_model = model
                if call_model and not conn.models:
                    # no models configured for this connection at all —
                    # treat it as disabled, never attempt a call.
                    continue
                if call_model and call_model not in conn.models:
                    continue
                short = _short_provider(conn)
                used_model = call_model or (conn.models[0] if conn.models else "")
                start = time.perf_counter()
                full = []
                try:
                    fit = self._fit_messages(messages,
                                             self._catalog_model(call_model),
                                             max_tokens)
                    for chunk in conn.stream(fit, model=call_model,
                                             max_tokens=max_tokens):
                        full.append(chunk)
                        yield chunk
                except (ProviderError, TimeoutError) as e:
                    self._emit("astra_gateway.error", provider=short,
                               model=used_model,
                               reason=getattr(e, "message", None) or str(e),
                               op=op, trace=trace, terminal=False)
                    continue
                except Exception as e:
                    self._emit("astra_gateway.error", provider=short,
                               model=used_model,
                               reason=f"{type(e).__name__}: {e}",
                               op=op, trace=trace, terminal=False)
                    continue
                self._emit("astra_gateway.success", provider=short,
                           model=used_model,
                           latency_ms=round((time.perf_counter() - start) * 1000.0, 1),
                           op=op, trace=trace, terminal=True,
                           output=_gw_log_output("".join(full)))
                return
            self._emit("astra_gateway.error", provider="", model=model or "",
                       reason="no connection served the requested model",
                       op=op, trace=trace, terminal=True)
            return

        category, ranked = self._select_order(messages, max_tokens)
        self._emit("astra_gateway.request", category=category,
                   candidates=len(ranked), op=op, trace=trace,
                   input=_gw_log_input(messages))
        # §23 streaming recovery: once ANY chunk has reached the caller for
        # this call, we must never silently start a second, independent
        # stream on the next connection — that would concatenate an
        # unrelated model's full response after the partial one already
        # sent, or duplicate it outright. So a target is only ever retried
        # here while emitted_any is still False; once it flips True an
        # interruption ends the call cleanly instead of falling over.
        emitted_any = False
        for conn, target_model, health in ranked:
            start = time.perf_counter()
            full = []
            try:
                for chunk in conn.stream(
                        self._fit_messages(messages, target_model, max_tokens),
                        model=target_model.model_id, max_tokens=max_tokens):
                    emitted_any = True
                    full.append(chunk)
                    yield chunk
            except (ProviderError, TimeoutError) as e:
                self.routing_state.record_failure(target_model.provider,
                                                  target_model.model_id)
                reason = getattr(e, "message", None) or str(e)
                self._emit("astra_gateway.error", provider=target_model.provider,
                          model=target_model.model_id, reason=reason,
                          op=op, trace=trace, terminal=False)
                if emitted_any:
                    self._emit("astra_gateway.stream_interrupted",
                              provider=target_model.provider,
                              model=target_model.model_id, reason=reason,
                              partial_content_sent=True, op=op, trace=trace,
                              terminal=True, output=_gw_log_output("".join(full)))
                    return
                continue
            except Exception as e:
                self.routing_state.record_failure(target_model.provider,
                                                  target_model.model_id)
                reason = f"{type(e).__name__}: {e}"
                self._emit("astra_gateway.error", provider=target_model.provider,
                          model=target_model.model_id, reason=reason,
                          op=op, trace=trace, terminal=False)
                if emitted_any:
                    self._emit("astra_gateway.stream_interrupted",
                              provider=target_model.provider,
                              model=target_model.model_id, reason=reason,
                              partial_content_sent=True, op=op, trace=trace,
                              terminal=True, output=_gw_log_output("".join(full)))
                    return
                continue
            latency_ms = (time.perf_counter() - start) * 1000.0
            self.routing_state.record_success(target_model.provider,
                                              target_model.model_id, latency_ms)
            self.last_connection = conn.name
            self.last_model = target_model.model_id
            self._emit("astra_gateway.success", provider=target_model.provider,
                      model=target_model.model_id, latency_ms=round(latency_ms, 1),
                      op=op, trace=trace, terminal=True,
                      output=_gw_log_output("".join(full)))
            return
        self.last_connection = ""
        self.last_model = ""
        self._emit("astra_gateway.error", provider="", model="",
                   reason="no suitable provider+model available",
                   op=op, trace=trace, terminal=True)

    # -- manual health probe ("🔌 AI Providers health" test buttons) ----------
    # Sends one tiny real request to a connection's first model, purely to
    # measure current latency/health — completely separate from `chat()`'s
    # routing loop (no fallback across connections here: a manual test of
    # "Cloudflare" must test Cloudflare, never quietly succeed via Bedrock).
    # Every result is persisted through `routing_state.record_success/
    # failure` the instant it's known — one connection's slow/failed probe
    # never delays saving another connection's result.
    _TEST_MESSAGES = [{"role": "user", "content": "ping"}]

    def classify_image_operation(self, message: str, attachments=None) -> str:
        """Gateway-owned deterministic modality/operation decision."""
        from astra.ai.gateway_routing import classify_gateway_request
        has_image = any(
            isinstance(a, dict) and a.get("family") == "image"
            and a.get("role") != "mask"
            for a in (attachments or []))
        has_mask = any(
            isinstance(a, dict) and a.get("family") == "image"
            and a.get("role") == "mask"
            for a in (attachments or []))
        return classify_gateway_request(
            message, vision=has_image, image_input=has_image,
            mask_input=has_mask)

    # -- image generation: eligible targets + failover -----------------------
    #
    # NOTE: image-model-pool construction is OWNED by `ImageRouter` (see
    # astra/ai/image_router.py, `ImageRouter._catalog` / `.build_targets`).
    # These two methods are kept ONLY as thin, backward-compatible
    # delegating wrappers for existing callers/tests that still hold a
    # Gateway reference and call `gateway.image_targets(...)` /
    # `gateway._image_catalog(...)` directly -- the Gateway itself
    # constructs NO image model pool of its own.
    def _image_catalog(self, *, discover: bool = True) -> list:
        return self.image_router._catalog(discover=discover)

    def image_targets(self, *, editing: bool = False,
                      operation: str | None = None,
                      discover: bool = True) -> list:
        return self.image_router.build_targets(
            editing=editing, operation=operation, discover=discover)

    @staticmethod
    def _image_failure_reason(exc) -> str:
        code = getattr(exc, "code", None)
        text = str(getattr(exc, "message", "") or exc).lower()
        if code == 429 or "rate limit" in text or "429" in text:
            return "429 rate limit"
        if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text:
            return "timeout"
        if code in (500, 501, 502, 503, 504) or "temporary" in text:
            return f"provider error ({code})" if code else "provider error"
        if code in (401, 403) or "auth" in text or "credential" in text:
            return "provider authentication/credential failure"
        if code == 404 or "not found" in text:
            return "model unavailable"
        if "quota" in text or "exhausted" in text or "billing" in text:
            return "quota exhausted"
        return text[:120] or "unknown error"

    def generate_image(self, prompt: str, model: str | None = None,
                       size: str = "1024x1024", n: int = 1, *,
                       editing: bool = False, source_image: dict | None = None,
                       mask_image: dict | None = None,
                       operation: str | None = None,
                       trace: str = "", discover: bool = True) -> str:
        """Backward-compatible delegating wrapper -- NOT an execution path.

        AstraAIGateway is the entry/classification/handoff layer; it must
        never perform a provider image-API HTTP call itself. All image
        selection, provider-adapter dispatch, serial fallback and lifecycle
        logging is owned by `ImageRouter` (see astra/ai/image_router.py).
        This method exists only for callers that still hold a Gateway
        reference and expect `gateway.generate_image(...)` to work; it does
        nothing but hand the request straight to `self.image_router`. New
        callers (ChatPipeline included) should call
        `gateway.image_router.generate(...)` directly.
        """
        return self.image_router.generate(
            prompt, model=model, size=size, n=n, editing=editing,
            source_image=source_image, mask_image=mask_image,
            operation=operation, trace=trace, discover=discover)

    def test_connection_model(self, conn, model_id: str) -> dict:
        """Probe exactly ONE model of one connection and persist that one
        result immediately — same idea as AstraRouter.test_provider_model,
        so the UI can fire one request per model and show each result the
        instant it lands instead of waiting on the connection's whole
        model list.

        When this (connection, model) resolves to the same canonical
        upstream provider + credential + model as a Provider-side manual
        test (astra.ai.router.AstraRouter.test_provider_model) running
        concurrently or recently, the real upstream HTTP probe is shared
        through `self.shared_health` instead of firing a second identical
        request — see astra/ai/shared_health.py. Only this manual-test
        path is affected: normal chat routing/fallback is untouched."""
        pool = getattr(conn, "pool", None)
        identity, cred = (resolve_identity(pool, conn.name, model_id)
                          if self.shared_health is not None else (None, None))

        def _probe() -> dict:
            # Deliberately tiny output for a latency/health probe — this is
            # a connectivity test, not AI content, so it is the one place a
            # small explicit budget is correct (astra.ai.token_limits).
            start = time.perf_counter()
            try:
                conn.chat(self._TEST_MESSAGES, model=model_id, max_tokens=8)
            except (ProviderError, TimeoutError) as e:
                return {"ok": False,
                       "error": getattr(e, "message", None) or str(e),
                       "latency_ms": 0.0}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}",
                       "latency_ms": 0.0}
            return {"ok": True, "error": "",
                   "latency_ms": round((time.perf_counter() - start) * 1000.0, 1)}

        if identity is not None:
            # Pin the pool to the exact credential the shared identity was
            # resolved against, so — if this caller ends up owning the real
            # probe — the identity and the actual upstream call always
            # agree on which key is being tested.
            pin = (pool.pinned(cred.key_id)
                  if pool is not None and cred is not None and hasattr(pool, "pinned")
                  else None)

            def _probe_fn():
                if pin is not None:
                    with pin:
                        return _probe()
                return _probe()

            result, reused = self.shared_health.run(identity, _probe_fn)
        else:
            result, reused = _probe(), False

        ok = bool(result.get("ok"))
        error = result.get("error") or ""
        latency_ms = result.get("latency_ms") or 0.0
        if ok:
            self.routing_state.record_success(conn.name, model_id, latency_ms)
        else:
            self.routing_state.record_failure(conn.name, model_id)
        self._emit("astra_gateway.test", connection=conn.name, model=model_id,
                   ok=ok, latency_ms=round(latency_ms, 1), reason=error,
                   reused=reused)
        return {"model": model_id, "ok": ok, "error": error,
                "latency_ms": round(latency_ms, 1)}

    def test_connection(self, conn) -> dict:
        """Probe EVERY model this Gateway connection exposes — not just the
        first — and persist each result immediately. Returns a small
        JSON-safe dict for the UI. Kept for the "test all" bulk endpoint;
        the per-model UI test calls `test_connection_model` directly, one
        model at a time, so each result can reach the browser as soon as
        it's ready instead of waiting for this whole list."""
        models = list(conn.models or [])
        if not models:
            return {"connection": conn.name, "ok": False,
                    "error": "no model configured", "models": []}
        results = [self.test_connection_model(conn, model_id) for model_id in models]
        return {"connection": conn.name, "ok": any(r["ok"] for r in results),
                "models": results, "error": ""}

    def test_connection_by_name(self, name: str) -> dict:
        """Probe exactly one connection by its name (e.g. 'astra-gw-gemini'),
        or every model of every connection sharing that name."""
        conn = next((c for c in self.connections if c.name == name), None)
        if conn is None:
            return {"connection": name, "ok": False, "models": [],
                    "error": "unknown gateway connection"}
        return self.test_connection(conn)

    def test_connection_model_by_name(self, name: str, model_id: str) -> dict:
        """Probe exactly one (connection, model) pair by connection name —
        backs the per-model UI test call."""
        conn = next((c for c in self.connections if c.name == name), None)
        if conn is None:
            return {"model": model_id, "ok": False, "latency_ms": 0,
                    "error": "unknown gateway connection"}
        return self.test_connection_model(conn, model_id)

    def test_all_connections(self) -> list:
        """Probe every configured connection's every model, one at a time.
        Each result is saved (via `test_connection`) the moment it
        completes — a slow or failing connection never blocks the others
        from being recorded, and the caller doesn't have to wait for every
        probe to *succeed*, only for the loop to finish."""
        return [self.test_connection(c) for c in self.connections]

    # -- health (reported separately, never as a provider) --------------------
    def health(self) -> dict:
        model_health_by_conn: dict[str, dict] = {}
        for conn, model in self._catalog:
            h = self.routing_state.get_health(model.provider, model.model_id)
            model_health_by_conn.setdefault(conn.name, {})[model.model_id] = h.to_dict()
        out = {}
        for c in self.connections:
            pool = getattr(c, "pool", None)
            if pool is not None:
                state = ("healthy" if bool(pool)
                         else ("not_configured" if pool.count == 0 else "degraded"))
            else:
                state = "not_configured"
            out[c.name] = {
                "state": state,
                "models": list(c.models or []),
                "base_url": getattr(c, "base_url", ""),
                "model_health": model_health_by_conn.get(c.name, {}),
            }
        return out

    def routing_status(self) -> dict:
        """Gateway-routing-specific status: last successful target + every
        tracked (provider, model) health row. Separate from `health()` so
        that method's shape (consumed by AstraRouter.gateway_health()) never
        has to change."""
        return {
            "last_category": self.last_category,
            "last_connection": self.last_connection,
            "last_model": self.last_model,
            "last_attempts": self.last_attempts,
            **self.routing_state.snapshot(),
        }

    # ═══════════════════════════════════════════════════════════════════
    # §3-§5, §18: Gateway recovery API for the EXISTING Provider system.
    #
    # These four methods are the entire surface the Provider system needs
    # to let the Gateway own routing/recovery *decisions* while the
    # Provider system keeps owning actual execution + credentials (§2,
    # §15, §21). Every argument/return value here is a
    # `ProviderExecutionTarget` (astra/ai/gateway_contract.py) — plain
    # provider_id/model_id/capabilities metadata. No adapter, credential,
    # HTTP client or ProviderRegistry object ever crosses this boundary.
    # ═══════════════════════════════════════════════════════════════════
    def select_execution_target(self, candidates, *, required_capabilities=(),
                                exclude=None):
        """Pick the best eligible (provider_id, model_id) target for the
        Existing Provider system to execute against, or None if nothing in
        `candidates` is currently eligible (out of cooldown, capability
        match, not already excluded)."""
        return self.execution_recovery.select_execution_target(
            candidates, required_capabilities=required_capabilities,
            exclude=exclude)

    def report_execution_success(self, target, latency_ms: float = 0.0, *,
                                 op: str = "", trace: str = "") -> None:
        """Record that `target` (a ProviderExecutionTarget) just succeeded —
        clears its cooldown and becomes the new soft last-successful
        preference (§12). `op`/`trace` (optional) are the calling route
        operation's correlation ids, so its success event refines that
        operation's Activity Log row instead of appearing as an orphan."""
        self.execution_recovery.report_execution_success(
            target, latency_ms, op=op, trace=trace)

    def report_execution_failure(self, target, category: str,
                                 cooldown_s: float | None = None, *,
                                 op: str = "", trace: str = "") -> None:
        """Record that `target` just failed with §7 category `category` —
        cools down that (provider, model) pair only (§6/§8), never the
        whole provider. `op`/`trace` correlate the failure event with the
        route operation that reported it."""
        self.execution_recovery.report_execution_failure(
            target, category, cooldown_s=cooldown_s, op=op, trace=trace)

    def recover_execution_target(self, candidates, failed_target, category, *,
                                 required_capabilities=(), exclude=None,
                                 op: str = "", trace: str = ""):
        """Report `failed_target`'s failure, then select the next suitable
        target from `candidates` (excluding `failed_target` and anything in
        `exclude`) — the single call the Existing Provider system needs on
        a mid-task AI-provider failure to keep going without restarting the
        task (§5, §9)."""
        return self.execution_recovery.recover_execution_target(
            candidates, failed_target, category,
            required_capabilities=required_capabilities, exclude=exclude,
            op=op, trace=trace)

    # ═══════════════════════════════════════════════════════════════════
    # §6-§12: Gateway-OWNED result supervision for the EXISTING Provider
    # system. Distinct from the four methods above: those decide WHO to
    # execute against; this decides whether WHAT came back is acceptable,
    # and — if not — drives a bounded correction round-trip to the SAME
    # target through the caller's `ProviderExecutionPort`. Still never an
    # adapter/credential/ProviderRegistry object crossing this boundary:
    # only `ProviderExecutionTarget`/`ProviderExecutionResult` plain data
    # and the caller-supplied port.
    # ═══════════════════════════════════════════════════════════════════
    def supervise_execution(self, port, target, messages, result, *,
                            max_tokens: int | None = None, require_json: bool = False,
                            required_fields: tuple = ()):
        """Validate `result` (a ProviderExecutionResult); if invalid/partial,
        send a correction back through `port` to `target` and validate again
        (bounded — astra.core.correction.MAX_CORRECTION_ATTEMPTS). Returns
        `(ProviderExecutionResult, ExecutionValidationOutcome)`."""
        return self.result_supervision.supervise(
            port, target, messages, result, max_tokens=max_tokens,
            require_json=require_json, required_fields=required_fields)

    def supervise_task(self, port, target, messages, result, contract, *,
                       evidence: dict | None = None, semantic_verifier=None,
                       max_tokens: int | None = None):
        """Validate `result` (a ProviderExecutionResult) against a
        `TaskCompletionContract` (§1-§8): deterministic shape, then
        evidence, then an optional semantic verifier. If not COMPLETE,
        sends a precise correction back through `port` to `target` and
        re-verifies (bounded — astra.core.correction.MAX_CORRECTION_ATTEMPTS).
        Returns `(ProviderExecutionResult, TaskVerificationOutcome, attempts)`.
        Never claims COMPLETE unless the final verification actually said so.
        """
        return self.task_completion.supervise(
            port, target, messages, result, contract, evidence=evidence,
            semantic_verifier=semantic_verifier, max_tokens=max_tokens)

    # -- shared agent tool loop (Gateway as the driving brain) ---------------
    def run_tool_loop(self, task, *, registry, system_prompt="", history=None,
                      context_blocks=None, terminal=None, runtime=None,
                      session_id=None,
                      scope=None, execution_history=None,
                      max_steps: int = 8, max_tokens: int | None = None,
                      trace: str = ""):
        """Drive the shared `AgentToolLoop` with the Gateway's own AI
        connections. The tools it can call are the same `ToolRegistry`
        tools — including the shared Terminal — that the Provider path
        uses, so both brains execute identical capabilities.

        Returns an `astra.ai.agent_tool_loop.ToolLoopResult`."""
        from astra.ai.agent_tool_loop import AgentToolLoop, GatewayToolCaller
        loop = AgentToolLoop(registry, terminal=terminal, runtime=runtime,
                             events=self.events,
                             max_steps=max_steps,
                             execution_history=execution_history)
        return loop.run(task, GatewayToolCaller(self),
                        system_prompt=system_prompt, history=history,
                        context_blocks=context_blocks, session_id=session_id,
                        scope=scope, max_tokens=max_tokens, trace=trace)


def build_astra_ai_gateway(config=None, store=None,
                          events=None) -> "AstraAIGateway":
    """Build the Astra AI Gateway — always, unconditionally.

    Mandatory-entry fix: this used to return `None` when no GW_* connection
    credentials were configured, which silently removed the ENTIRE Gateway
    control layer from the router (`AstraRouter.gateway is None` skips
    `_route_via_gateway` completely — no target selection/recovery, no
    Task Completion Contract verification/correction at all, for every
    request, forever, on a totally ordinary "nobody set GW_GEMINI_API_KEYS"
    deployment). That conflated two independent things:

      1. The Gateway's own GW_* AI connections — used ONLY by
         `GatewayRequestIntelligence` to enrich/rewrite the raw request
         text. Genuinely optional: enrichment fails open to the original
         text when no connection is configured (see `process()` below),
         because improving wording needs an actual model call.
      2. The Gateway's control/governance layer — `select_execution_target`/
         `recover_execution_target` (routing decisions) and `supervise_task`
         (Task Completion Contract verify/correct/re-verify). NONE of this
         needs a GW_* connection: it is deterministic contract logic plus
         calls back through the CALLER's own `ProviderExecutionPort` into
         the Existing Provider System (`self.connections` is never touched
         by any of `execution_recovery`/`result_supervision`/
         `task_completion` — see `AstraAIGateway.__init__` below).

    So (2) — the actual "Astra AI Gateway is the mandatory entry/control
    point for every normal user request" requirement — must never depend
    on whether anyone happened to configure (1). This builder therefore
    always returns a real `AstraAIGateway` instance; `gw.connections` is
    simply `[]` when unconfigured, `gw.is_usable()` is `False` (so request
    enrichment still, correctly, passes text through unchanged), and
    `gateway_health()` still reports `"not_configured"` for zero
    connections exactly as before this fix — but `AstraRouter.gateway` is
    never `None`, so `route_request()` always goes through
    `_route_via_gateway` and any request carrying a `task_contract` always
    gets real Gateway-owned verification. See `AstraRouter.route_request`
    for the second half of this fix (a `task_contract` with no Gateway at
    all now fails closed instead of silently skipping verification — that
    combination is what actually cannot happen once this builder never
    returns `None`, but the check stays there as a defense-in-depth
    safety net for any future caller that constructs `AstraRouter` without
    wiring this builder's result in).

    `store`, when given, is the project's existing SQLite Store — used
    only to persist last-successful-target and per-model health (§14)
    across restarts; no new database is introduced, and no credentials
    are ever written to it.
    """
    from astra.core.config import Config
    config = config or Config()
    return AstraAIGateway(config=config, store=store, events=events)


# ═══════════════════════════════════════════════════════════════════════════
# Request Intelligence — the Gateway as an AI Request Intelligence Layer
# ═══════════════════════════════════════════════════════════════════════════
#
#     User -> Assistant -> Astra AI Gateway -> Request Understanding
#          -> Request Enrichment/Structuring -> Provider-Ready Prompt
#          -> Existing Provider System -> Provider AI -> Final Response
#          -> Assistant -> User
#
# The Gateway's only job here is to turn a raw, possibly short/incomplete/
# poorly-structured/mixed-language user message into a clearer, better
# structured instruction — using its OWN connections (GW_* config,
# AstraAIGateway.chat() above) — and hand that improved text back to the
# caller. It never calls a Provider adapter, never touches ProviderRegistry
# or AstraRouter, and never performs the user's actual task itself: the
# existing Provider system remains the sole executor. Gateway -> Provider
# (as a plain text handoff, done by the caller) is fine; Provider -> Gateway
# and Gateway -> ProviderRegistry are both absent by design, same as the
# rest of this module.

# Sentinel the Request Understanding model outputs when it decides a
# message needs no rewrite at all — the common case. This is how the
# Gateway "decides" up front whether improvement is even needed, instead
# of always paying for (and risking) a rewrite on every single message.
NO_CHANGE_TOKEN = "NO_CHANGE_NEEDED"

_GATEWAY_UNDERSTANDING_SPECIALIZED_PROMPT = (
    "You are the Astra AI Gateway's Request Understanding layer. You do NOT "
    "answer the user's request and you do NOT perform the task yourself — a "
    "separate Provider AI does that after you, using only what you output. "
    "Your only job is to turn the user's raw message (which may be short, "
    "incomplete, poorly structured, or mixed Bengali/Banglish/English) into "
    "a clear, well-structured instruction for that Provider AI to execute.\n\n"
    "Rules:\n"
    "- Preserve the user's original intent and goal exactly — never change "
    "what they actually asked for.\n"
    "- Preserve important wording, numbers, names, and explicit "
    "requirements from the message.\n"
    "- Identify constraints already present in the request and keep them.\n"
    "- Organize unclear or fragmented instructions into clear structure.\n"
    "- Add useful structure or context only when it can be safely inferred "
    "from the message itself. NEVER invent facts, credentials, "
    "requirements, or user intentions that are not present in the message.\n"
    "- If information needed to complete the task is genuinely missing and "
    "cannot be safely inferred, say plainly that it is missing instead of "
    "guessing or making it up.\n"
    "- Match the amount of structure to the request: a simple message "
    "(e.g. \"hello\") gets a short, simple rewrite — do not produce a large "
    "structured prompt for a simple request. A complex or unclear task "
    "gets useful structure (for example USER TASK / OBJECTIVE / "
    "INSTRUCTIONS sections).\n"
    "- You may also receive a block of recent prior conversation, for "
    "understanding only. Use it strictly to resolve references in the "
    "current message (e.g. \"eita\", \"that one\", \"amar age bola ta\") "
    "and to keep meaning consistent with what came before. Never pull new "
    "instructions, facts, or requirements out of the prior conversation "
    "that the current message does not actually reference, and never let "
    "it override or expand what the current message asks for.\n"
    "- CRITICAL: you are rewriting the USER's message, in the user's voice "
    "— you are never the one replying to it. NEVER greet back, NEVER write "
    "as if you are the assistant answering (e.g. \"Hello! How can I assist "
    "you today?\"), and NEVER ask the user a question yourself. A bare "
    "greeting like \"hi\" or \"hello\" stays a greeting — the correct "
    "rewrite is simply \"Greet the user.\" or the original word itself, not "
    "a reply to it.\n"
    "- FIRST decide: does this message actually need rewriting/structuring "
    "for the Provider AI, or is it already clear, complete, and unambiguous "
    "as-is? Most messages do NOT need a rewrite. If it does not need any "
    "change, output exactly the single token " + NO_CHANGE_TOKEN + " and "
    "nothing else — do not restate or reformat the message. Only produce a "
    "rewritten version when it genuinely needs clarifying, translating, or "
    "structuring.\n"
    "- Output ONLY the rewritten request text for the Provider AI, or the "
    "single token " + NO_CHANGE_TOKEN + " — no preamble, no explanation, no "
    "meta-commentary about what you changed."
)

GATEWAY_UNDERSTANDING_SYSTEM_PROMPT = build_system_prompt(
    _GATEWAY_UNDERSTANDING_SPECIALIZED_PROMPT)

# No Astra-imposed output cap: unset => the Gateway connection's
# model decides (see astra.ai.token_limits).
GW_UNDERSTANDING_MAX_TOKENS = None

# Guard against the Request Understanding model slipping into
# assistant-voice (answering the message instead of rewriting it) — most
# visible on bare greetings, where "hi" can come back as "Hello! How can I
# assist you today?". If the "rewrite" reads like the assistant replying
# rather than the user's own request restated, it is not safe to hand to
# the chat pipeline as the goal, so we fail open to the original raw text
# instead of silently feeding the hallucination downstream.
_ASSISTANT_VOICE_MARKERS = (
    "how can i assist", "how can i help", "how may i assist",
    "how may i help", "what can i do for you", "how can i be of",
    "i'm here to help", "i am here to help",
)


def _looks_like_assistant_voice(text: str) -> bool:
    t = text.lower()
    return any(marker in t for marker in _ASSISTANT_VOICE_MARKERS)


# ═══════════════════════════════════════════════════════════════════════════
# Intent classification — the Gateway decides what's even a task
# ═══════════════════════════════════════════════════════════════════════════
#
# Not every message is a goal. A greeting, "thanks", small talk, or a vague
# opener has no task in it, and forcing it through the full plan -> Provider
# pipeline is exactly how internal machinery ends up leaking to the user
# (e.g. a raw "your goal is a greeting" reply). That decision
# belongs to the Gateway, once, up front — not to the Provider
# guessing after the fact. `classify()` is that decision: it never plans,
# never picks a tool, and fails open (treats anything uncertain as a real
# task) so a genuine request is never silently swallowed.

_GATEWAY_CLASSIFY_SPECIALIZED_PROMPT = (
    "You are the Astra AI Gateway's Intent Classifier. Decide whether the "
    "user's message is:\n"
    "  (a) small talk — a greeting, thanks, \"how are you\", or a vague "
    "opener with nothing for the assistant to actually do, or\n"
    "  (b) an actual request/task/question the assistant should act on.\n\n"
    "Respond with ONLY one JSON object, no markdown fence, no preamble, no "
    "explanation:\n"
    "  If (a): {\"is_task\": false, \"reply\": \"<a short, warm, natural "
    "reply to the message itself, in the same language/tone the user "
    "used>\"}\n"
    "  If (b): {\"is_task\": true}\n\n"
    "Rules:\n"
    "- A short factual question or any ask the assistant could act on IS a "
    "task, even if brief (e.g. \"btc price?\" is a task).\n"
    "- Greetings, thanks, and small talk are NOT tasks.\n"
    "- When genuinely unsure, choose is_task: true — never misclassify a "
    "real request as small talk.\n"
    "- The \"reply\" (when used) answers the user directly — it is not a "
    "description of what you decided.\n"
    "- Output ONLY the JSON object."
)

GATEWAY_CLASSIFY_SYSTEM_PROMPT = build_system_prompt(
    _GATEWAY_CLASSIFY_SPECIALIZED_PROMPT)

# Unset => model-decided, same as above.
GW_CLASSIFY_MAX_TOKENS = None


class GatewayRequestIntelligence:
    """Request Understanding / Enrichment: the Gateway's preprocessing step.

    `process()` first lets the Gateway decide, itself, whether the raw
    message even needs improving — most messages don't. Only when it
    genuinely needs clarifying/structuring does the Gateway rewrite it;
    otherwise the original text is handed to the Provider system
    unchanged (see `NO_CHANGE_TOKEN`). Sent to the Gateway's own AI
    connections (via `AstraAIGateway.chat()` — Gemini -> Groq -> Cloudflare
    -> Bedrock fallback, GW_* config only). This class does not execute the
    task and is never registered as a Provider: callers hand its output to
    the existing Provider system (e.g. `AstraRouter.route(...)`) for actual
    execution.

    Fails open: if the Gateway is absent, unconfigured, or every connection
    errors, `process()` returns the original text unchanged
    (`enriched=False`) rather than inventing content or blocking the
    request — the Assistant -> Provider path keeps working exactly as it
    did before this layer existed.
    """

    def __init__(self, gateway: "AstraAIGateway | None"):
        self.gateway = gateway

    def is_usable(self) -> bool:
        return self.gateway is not None and self.gateway.is_usable()

    def process(self, raw_text: str, *,
               max_tokens: int | None = GW_UNDERSTANDING_MAX_TOKENS,
               context: str = "") -> dict:
        """Understand + structure `raw_text` into a Provider-ready prompt.

        `context`, when given, is recent prior Assistant conversation
        relevant to `raw_text` (e.g. what the current short message refers
        back to). It is passed to the Gateway purely so it can resolve
        references in `raw_text` — it is never treated as a new request of
        its own, never echoed back verbatim, and is dropped entirely when
        empty (the request/response shape is identical to calling without
        it, so existing callers are unaffected).

        Returns {"text": str, "enriched": bool, "gateway_connection": str,
        "raw_text": str}. `text` is always safe to hand straight to the
        Provider system: either the Gateway's improved version, or (on any
        failure/absence) the original raw text.
        """
        text = " ".join(str(raw_text or "").split()).strip()
        if not text or not self.is_usable():
            return {"text": text, "enriched": False,
                    "gateway_connection": "", "raw_text": text}
        ctx = " ".join(str(context or "").split()).strip()
        messages = [
            {"role": "system", "content": GATEWAY_UNDERSTANDING_SYSTEM_PROMPT},
        ]
        if ctx:
            messages.append({"role": "user", "content":
                             "Relevant prior conversation (context only — "
                             "do not treat as a new request):\n" + ctx})
        messages.append({"role": "user", "content": text})
        try:
            improved = self.gateway.chat(messages, max_tokens=max_tokens)
        except Exception:
            return {"text": text, "enriched": False,
                    "gateway_connection": "", "raw_text": text}
        improved = (improved or "").strip()
        if not improved or improved == "(no reply)":
            return {"text": text, "enriched": False,
                    "gateway_connection": "", "raw_text": text}
        if improved.strip('"\'` \n') == NO_CHANGE_TOKEN:
            # The Gateway decided the message is already clear enough —
            # this IS the decision the user asked for ("improve kora
            # lagbe naki lagbe na"): pass the original straight to the
            # Provider, unchanged, rather than manufacturing a rewrite
            # nobody needs.
            return {"text": text, "enriched": False,
                    "gateway_connection": "", "raw_text": text}
        if _looks_like_assistant_voice(improved):
            # The model answered the message instead of rewriting it (e.g.
            # "hi" -> "Hello! How can I assist you today?"). Handing that
            # to the pipeline as the "goal" would make it look like the
            # assistant's own greeting is the user's request — fail open
            # to the original raw text instead.
            return {"text": text, "enriched": False,
                    "gateway_connection": "", "raw_text": text}
        return {"text": improved, "enriched": True,
                "gateway_connection": self.gateway.last_connection,
                "raw_text": text}

    def classify(self, raw_text: str, *, context: str = "",
                max_tokens: int | None = GW_CLASSIFY_MAX_TOKENS) -> dict:
        """Decide whether `raw_text` is an actual task or just small talk —
        the Gateway managing this up front, so the chat pipeline never has
        to guess and never forces a greeting/thanks/vague opener through the
        full Provider pipeline.

        Returns {"is_task": bool, "reply": str, "classified": bool}.
        `classified` is False whenever the Gateway is absent, unusable,
        errors, or replies with something unparsable — and `is_task`
        defaults to True in every one of those cases, so a real request is
        never silently dropped just because classification failed. Only a
        confident, parsed "not a task" verdict (with actual reply text to
        use) sets `is_task` False.
        """
        text = " ".join(str(raw_text or "").split()).strip()
        if not text or not self.is_usable():
            return {"is_task": True, "reply": "", "classified": False}
        ctx = " ".join(str(context or "").split()).strip()
        messages = [{"role": "system", "content": GATEWAY_CLASSIFY_SYSTEM_PROMPT}]
        if ctx:
            messages.append({"role": "user", "content":
                             "Relevant prior conversation (context only — "
                             "do not treat as a new request):\n" + ctx})
        messages.append({"role": "user", "content": text})
        try:
            raw = self.gateway.chat(messages, max_tokens=max_tokens)
        except Exception:
            return {"is_task": True, "reply": "", "classified": False}
        raw = (raw or "").strip()
        if not raw or raw == "(no reply)":
            return {"is_task": True, "reply": "", "classified": False}
        from astra.ai.json_extract import loads_lenient
        try:
            data = loads_lenient(raw)
        except Exception:
            return {"is_task": True, "reply": "", "classified": False}
        if not isinstance(data, dict) or "is_task" not in data:
            return {"is_task": True, "reply": "", "classified": False}
        is_task = bool(data.get("is_task", True))
        if is_task:
            return {"is_task": True, "reply": "", "classified": True}
        reply = str(data.get("reply") or "").strip()
        if not reply:
            # Said small talk but gave nothing to answer with — not safe
            # to short-circuit on an empty reply, fail open to a real task.
            return {"is_task": True, "reply": "", "classified": False}
        return {"is_task": False, "reply": reply, "classified": True}


def build_gateway_request_intelligence(
        gateway: "AstraAIGateway | None") -> GatewayRequestIntelligence:
    """Wrap a (possibly None) Gateway in the request-intelligence layer.

    Always returns an object — `GatewayRequestIntelligence.process()`
    degrades gracefully to a pass-through when `gateway` is None or
    unusable, so callers never need a None-check of their own.
    """
    return GatewayRequestIntelligence(gateway)
