"""Live API test + structured health reporting for the agentrouter.org
gateway client (astra/ai/agentrouter_gateway.py).

This is deliberately separate from `AgentRouter.gateway_health()` (router.py),
which only reports *configuration* state. Everything in this module makes a
REAL authenticated HTTP request when credentials are configured — it never
mocks or fabricates a result — and classifies the outcome into one of
FAILURE_STATES so a caller (the /api/agentrouter/health endpoint, the
dashboard "Test AgentRouter API" button) can tell "not configured" apart
from "configured but unreachable" apart from "reachable but unauthenticated"
apart from "working".

No secret ever appears in a returned dict: only counts, booleans, safe HTTP
status labels and latency leave this module. Callers that expose these
results over the API additionally pass through astra.security.redact as a
second line of defence.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

from astra.ai.credentials import CredentialPool

DEFAULT_BASE_URL = "https://agentrouter.org/v1"
DEFAULT_TEST_MODEL = "deepseek-v4-flash"

# Every state this module can report. "SUCCESS" is the only non-failure.
FAILURE_STATES = (
    "NOT_CONFIGURED", "NETWORK_ERROR", "DNS_ERROR", "TIMEOUT",
    "HTTP_401", "HTTP_403", "HTTP_404", "HTTP_429", "HTTP_5XX",
    "INVALID_RESPONSE", "SUCCESS",
)

_TEST_TIMEOUT_S = 15
_TEST_MAX_TOKENS = 8
_TEST_MESSAGES = [{"role": "user", "content": "ping"}]

_SAFE_DETAIL = {
    "NOT_CONFIGURED": "AgentRouter API: not configured",
    "NETWORK_ERROR": "AgentRouter API: network error",
    "DNS_ERROR": "AgentRouter API: DNS resolution failed",
    "TIMEOUT": "AgentRouter API: request timed out",
    "HTTP_401": "AgentRouter API: Unauthorized (401)",
    "HTTP_403": "AgentRouter API: Forbidden (403)",
    "HTTP_404": "AgentRouter API: model or route not found (404)",
    "HTTP_429": "AgentRouter API: rate limited (429)",
    "HTTP_5XX": "AgentRouter API: upstream server error (5xx)",
    "INVALID_RESPONSE": "AgentRouter API: response was not a usable model reply",
    "SUCCESS": "ok",
}


def _classify_http_error(e: urllib.error.HTTPError) -> str:
    code = getattr(e, "code", 0)
    if code == 401:
        return "HTTP_401"
    if code == 403:
        return "HTTP_403"
    if code == 404:
        return "HTTP_404"
    if code == 429:
        return "HTTP_429"
    if 500 <= code < 600:
        return "HTTP_5XX"
    return "NETWORK_ERROR"


def _one_request(base_url: str, api_key: str, model: str,
                  timeout: float = _TEST_TIMEOUT_S) -> tuple[str, int, str]:
    """One real POST to {base_url}/chat/completions.

    Returns (status, latency_ms, safe_detail). Never raises and never
    includes the api_key (or any part of it) in the returned detail.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = json.dumps({"model": model, "messages": _TEST_MESSAGES,
                       "max_tokens": _TEST_MAX_TOKENS}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "content-type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = _classify_http_error(e)
        return status, int((time.perf_counter() - t0) * 1000), _SAFE_DETAIL[status]
    except socket.timeout:
        return "TIMEOUT", int((time.perf_counter() - t0) * 1000), _SAFE_DETAIL["TIMEOUT"]
    except urllib.error.URLError as e:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.gaierror):
            return "DNS_ERROR", latency_ms, _SAFE_DETAIL["DNS_ERROR"]
        if isinstance(reason, socket.timeout):
            return "TIMEOUT", latency_ms, _SAFE_DETAIL["TIMEOUT"]
        return "NETWORK_ERROR", latency_ms, _SAFE_DETAIL["NETWORK_ERROR"]
    latency_ms = int((time.perf_counter() - t0) * 1000)
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return "INVALID_RESPONSE", latency_ms, _SAFE_DETAIL["INVALID_RESPONSE"]
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return "INVALID_RESPONSE", latency_ms, _SAFE_DETAIL["INVALID_RESPONSE"]
    if content is None:
        return "INVALID_RESPONSE", latency_ms, _SAFE_DETAIL["INVALID_RESPONSE"]
    return "SUCCESS", latency_ms, _SAFE_DETAIL["SUCCESS"]


def _emit(events, kind: str, **data) -> None:
    if events is None:
        return
    try:
        events.emit(kind, agent="agentrouter_gateway", **data)
    except Exception:
        pass


def check_agentrouter(config, events=None, *, model: str | None = None,
                     test_all_models: bool = False,
                     test_all_keys: bool = False) -> dict:
    """Run a real live-API test against agentrouter.org.

    `config` is an astra.core.config.Config (or anything with .get/.getlist).
    Returns a structured, secret-free dict — see module docstring. When
    `test_all_keys`/`test_all_models` are set, adds `keys`/`models` breakdowns
    (values only ever "success"/"failed", never the credential itself).
    """
    base_url = (config.get("AGENTROUTER_BASE_URL", None) or DEFAULT_BASE_URL).rstrip("/")
    keys = config.getlist("AGENTROUTER_API_KEYS", default=[])
    models = config.getlist("AGENTROUTER_MODELS", default=[])
    test_model = model or (models[0] if models else DEFAULT_TEST_MODEL)

    if not keys:
        _emit(events, "agentrouter.error", status="NOT_CONFIGURED", model=test_model)
        return {
            "configured": False, "reachable": False, "authenticated": False,
            "status": "NOT_CONFIGURED", "base_url": base_url, "model": None,
            "latency_ms": None, "detail": _SAFE_DETAIL["NOT_CONFIGURED"],
        }

    pool = CredentialPool("agentrouter_gateway", keys)
    cred = pool.pick()
    _emit(events, "agentrouter.request", base_url=base_url, model=test_model)

    if cred is None:
        # every key is unhealthy/in cooldown — still "configured", just not
        # currently usable
        _emit(events, "agentrouter.error", status="HTTP_401", model=test_model)
        return {
            "configured": True, "reachable": False, "authenticated": False,
            "status": "HTTP_401", "base_url": base_url, "model": test_model,
            "latency_ms": None,
            "detail": "no healthy AgentRouter credential available",
        }

    status, latency_ms, detail = _one_request(base_url, pool.get_secret_for(cred), test_model)
    if status == "SUCCESS":
        pool.report_success(cred)
        _emit(events, "agentrouter.success", model=test_model, latency_ms=latency_ms)
    else:
        pool.report_failure(
            cred, reason=status,
            auth_failure=status in ("HTTP_401", "HTTP_403"),
            rate_limited=status == "HTTP_429")
        _emit(events, "agentrouter.error", model=test_model, status=status,
              latency_ms=latency_ms)

    unreachable = status in ("NETWORK_ERROR", "DNS_ERROR", "TIMEOUT")
    unauthenticated = status in ("HTTP_401", "HTTP_403")
    result = {
        "configured": True,
        "reachable": not unreachable,
        "authenticated": not (unreachable or unauthenticated),
        "status": "ok" if status == "SUCCESS" else status,
        "base_url": base_url,
        "model": test_model,
        "latency_ms": latency_ms,
        "detail": detail,
    }

    if test_all_keys and len(keys) > 1:
        key_results = []
        for i, k in enumerate(keys, start=1):
            st, lat, _det = _one_request(base_url, k, test_model)
            key_results.append({
                "key": f"Key #{i}",
                "status": "success" if st == "SUCCESS" else "failed",
                "latency_ms": lat,
            })
        result["keys"] = key_results

    if test_all_models and models:
        model_results = []
        for m in models:
            st, lat, _det = _one_request(base_url, pool.get_secret_for(cred), m)
            model_results.append({
                "model": m, "working": st == "SUCCESS", "status": st,
                "latency_ms": lat,
            })
        result["models"] = model_results

    return result
