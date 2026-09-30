"""Read-only aggregate behind the Security Center page (GET /api/security/status).

Everything here is derived from live objects (config, rate limiter, tool
registry, provider router, Web3 policy, Agent Runtime, emergency latch).
A value the backend cannot report is returned as ``None`` (the UI renders
"Not reported by API") — nothing is estimated, and there is NO security
score: Astra has no deterministic scoring model, so none is invented.

Secret-free by construction: it reports whether a control is on and counts
of credentials, never a key, token, origin list or credential value.
"""
from __future__ import annotations

import os
import time

# Tool risk_level -> display bucket. Presentation only; the raw levels come
# from astra/core/permissions.py Level and are also returned untouched.
RISK_BUCKET = {
    "read": "low", "low_risk_write": "low",
    "browser_action": "medium",
    "financial_action": "high", "system_action": "high", "admin": "high",
}
_LOOPBACK = {"127.0.0.1", "localhost", "::1", ""}


def system_map_security(site) -> list[dict]:
    """The `security` rows of GET /api/system-map (moved here unchanged so
    the System Map and the Security Center read one implementation)."""
    tok = bool(site.operator_token)
    rl = site.rate_limiter
    origins = site._allowed_origins
    if origins:
        cors = "Allow-list (%d origin%s)" % (len(origins), "" if len(origins) == 1 else "s")
    elif site.env == "production":
        cors = "Same-origin only"
    else:
        cors = "Reflects request origin (development/LAN)"
    return [
        {"k": "Authentication",
         "v": "Operator token required" if tok else "Open (local-first, ASTRA_TOKEN unset)",
         "s": "online" if tok else "degraded"},
        {"k": "Rate limiting",
         "v": ("%d requests / %ds per client" % (rl.limit, int(rl.window))) if rl else "Disabled",
         "s": "online" if rl else "degraded"},
        {"k": "CORS", "v": cors, "s": "online" if (origins or site.env == "production") else "info"},
        {"k": "Request ID", "v": "X-Request-Id on every response", "s": "online"},
        {"k": "Secret redaction", "v": "Enabled (events, logs, API bodies)", "s": "online"},
        {"k": "SSRF protection", "v": "Enabled (private/loopback targets blocked)", "s": "online"},
        {"k": "Body size limit", "v": "%d MB" % (site.max_body_bytes // (1024 * 1024)), "s": "online"},
        {"k": "Environment", "v": site.env, "s": "info"},
    ]


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _row(k, v, level="ok"):
    return {"k": k, "v": v, "level": level}


def _bind_is_loopback(site) -> bool:
    cfg = site.cfg()
    host = (cfg.get("BIND", "127.0.0.1") if cfg else "127.0.0.1") or ""
    return str(host).strip().lower() in _LOOPBACK


# -- controls ---------------------------------------------------------------

def _controls(site) -> list[dict]:
    from astra import web as _web              # SECURITY_HEADERS lives there
    tok = bool(site.operator_token)
    rl = site.rate_limiter
    origins = site._allowed_origins
    headers = {h for h, _ in _web.SECURITY_HEADERS}
    csp = next((v for h, v in _web.SECURITY_HEADERS
                if h == "Content-Security-Policy"), "")
    if origins:
        cors = ("Configured", "ok", "Allow-list (%d origin%s)" % (
            len(origins), "" if len(origins) == 1 else "s"))
    elif site.env == "production":
        cors = ("Configured", "ok", "Same-origin only")
    else:
        cors = ("Permissive", "warn", "Reflects request origin (development/LAN)")

    def c(cid, name, status, level, detail):
        return {"id": cid, "name": name, "status": status,
                "level": level, "detail": detail}
    return [
        c("auth", "API Authentication",
          "Protected" if tok else "Open", "ok" if tok else "warn",
          "Operator token required on /api/*" if tok
          else "ASTRA_TOKEN is unset — local-first, no token required"),
        c("rate_limit", "Rate Limiting",
          "Active" if rl else "Disabled", "ok" if rl else "warn",
          ("%d requests / %ds per client" % (rl.limit, int(rl.window))) if rl
          else "No limiter configured"),
        c("cors", "CORS Policy", cors[0], cors[1], cors[2]),
        c("headers", "Security Headers",
          "Protected" if {"X-Content-Type-Options", "X-Frame-Options",
                          "Referrer-Policy"} <= headers else "Partial",
          "ok", "nosniff, frame-options, referrer-policy, COOP on hardened responses"),
        c("csp", "Content Security Policy",
          "Active" if csp else "Not set", "ok" if csp else "warn",
          csp or "No CSP header configured"),
        c("body_limit", "Request Size Limits", "Active", "ok",
          "%d MB request body cap" % (site.max_body_bytes // (1024 * 1024))),
        c("ssrf", "SSRF Protection",
          "Relaxed" if os.environ.get("ASTRA_ALLOW_PRIVATE_URLS") == "1" else "Protected",
          "warn" if os.environ.get("ASTRA_ALLOW_PRIVATE_URLS") == "1" else "ok",
          "Private URLs allowed by ASTRA_ALLOW_PRIVATE_URLS=1"
          if os.environ.get("ASTRA_ALLOW_PRIVATE_URLS") == "1"
          else "Private/loopback/metadata targets blocked, redirects re-checked"),
        c("request_id", "Request ID / Logging", "Active", "ok",
          "X-Request-Id on every response; secrets redacted in logs and API bodies"),
    ]


# -- providers / credentials ------------------------------------------------

def _providers(site) -> dict:
    router = site.router()
    if router is None:
        return {"available": False, "rows": [], "keys": None, "healthy": None,
                "failed": None, "providers": None}
    health = _safe(router.health, {}) or {}
    rows, keys_total, keys_ok, prov_with_keys = [], 0, 0, 0
    for name, p in sorted(health.items()):
        keys = p.get("keys") or []
        if not keys:
            continue                              # not configured: not listed
        prov_with_keys += 1
        ok = sum(1 for k in keys if k.get("healthy"))
        keys_total += len(keys)
        keys_ok += ok
        state = p.get("state") or "unknown"
        rows.append({"name": name, "state": state,
                     "level": "ok" if state == "healthy" else "warn",
                     "keys": len(keys), "healthy_keys": ok,
                     "models": len(p.get("models") or []), "kind": "provider"})
    gw = _safe(router.gateway_health, {}) or {}
    for name, conn in sorted((gw.get("connections") or {}).items()):
        state = (conn or {}).get("state") or "unknown"
        rows.append({"name": name, "state": state,
                     "level": "ok" if state == "healthy" else "warn",
                     "keys": None, "healthy_keys": None, "models": None,
                     "kind": "gateway"})
    return {"available": True, "rows": rows, "keys": keys_total,
            "healthy": keys_ok, "failed": keys_total - keys_ok,
            "providers": prov_with_keys, "gateway_state": gw.get("state")}


# -- tools ------------------------------------------------------------------

def _tools(site) -> dict:
    reg = site.registry()
    if reg is None:
        return {"available": False}
    tools = _safe(reg.list, []) or []
    stats = _safe(reg.stats, {}) or {}
    by_risk: dict = {}
    buckets = {"high": 0, "medium": 0, "low": 0}
    for t in tools:
        lvl = t.get("risk_level") or "read"
        by_risk[lvl] = by_risk.get(lvl, 0) + 1
        buckets[RISK_BUCKET.get(lvl, "low")] += 1
    policy = site._get("policy")
    granted = _safe(lambda: policy.describe().get("granted"), None) if policy else None
    return {
        "available": True, "total": len(tools), "by_risk": by_risk,
        "high": buckets["high"], "medium": buckets["medium"], "low": buckets["low"],
        "confirmation_required": sum(1 for t in tools if t.get("requires_confirmation")),
        "agent_forbidden": sum(1 for t in tools if t.get("agent_forbidden")),
        "calls": sum(s.get("calls", 0) for s in stats.values()),
        "errors": sum(s.get("errors", 0) for s in stats.values()),
        "counted": "since server start",       # registry stats are in-memory
        "granted": granted,
    }


# -- web3 -------------------------------------------------------------------

def _web3(site) -> dict:
    policy = site._get("web3_policy") or site._get("tx_policy")
    tx = site._get("tx_manager")
    if policy is None:
        return {"available": False}
    d = policy.describe()
    limits = bool(d.get("tx_limit_wei") or d.get("daily_limit_wei")
                  or d.get("gas_limit_max"))
    custom_chains = bool(getattr(policy.cfg, "allowed_chain_ids", None))
    return {
        "available": True, "mode": d.get("mode"),
        "stopped": bool(getattr(tx, "stopped", False)) if tx is not None else None,
        "chains": len(d.get("chains_allowed") or []),
        "chains_source": "configured" if custom_chains else "built-in default set",
        "limits_configured": limits,
        "recipient_allowlist": len(d.get("recipients_allowed") or []),
        "contract_allowlist": len(d.get("contracts_allowed") or []),
        "wallet_allowlist": len(d.get("wallets_allowed") or []),
    }


def _web3_rows(w) -> list[dict]:
    if not w.get("available"):
        return []
    mode = w.get("mode") or ""
    return [
        _row("Confirmation Mode", mode or None,
             "ok" if mode == "CONFIRM" else "warn"),
        _row("Emergency Stop",
             None if w["stopped"] is None else ("Engaged" if w["stopped"] else "Not engaged"),
             "danger" if w["stopped"] else "ok"),
        _row("Allowed Chains", "%d chains (%s)" % (w["chains"], w["chains_source"]),
             "ok" if w["chains_source"] == "configured" else "info"),
        _row("Transaction Limits",
             "Configured" if w["limits_configured"] else "Unlimited",
             "ok" if w["limits_configured"] else "warn"),
        _row("Policy Status", "Active", "ok"),
    ]


# -- runtime ------------------------------------------------------------------

def _runtime_rows(site, tools) -> list[dict]:
    rows = []
    rt_mgr = site._get("runtime")
    st = None
    if rt_mgr is not None:
        st = _safe(lambda: rt_mgr.get(None).status(refresh_capabilities=False))
    if st and st.get("available"):
        rows.append(_row("Agent Runtime",
                         "%s (%s)" % (st.get("state") or "unknown",
                                      st.get("backend") or "no backend"), "ok"))
        rows.append(_row("Process Isolation",
                         "Isolated" if st.get("host_isolation") else "Not isolated",
                         "ok" if st.get("host_isolation") else "warn"))
        if st.get("workspace"):
            rows.append(_row("File System Access",
                             "Workspace: %s" % st["workspace"], "info"))
    elif st:
        # An unavailable runtime cannot vouch for isolation or file scope —
        # report only what is true: it is not running.
        rows.append(_row("Agent Runtime", "Unavailable", "warn"))
        rows.append(_row("Process Isolation", None, "na"))
    else:
        rows.append(_row("Agent Runtime", None, "na"))
        rows.append(_row("Process Isolation", None, "na"))
    if tools.get("available") and tools.get("agent_forbidden") is not None:
        approvals = site._get("approvals") is not None
        rows.append(_row(
            "Terminal Execution",
            "Host terminal blocked for agents"
            + ("; approval required" if approvals else ""),
            "ok" if tools["agent_forbidden"] else "warn"))
    cfg = site.cfg()
    bmode = _safe(lambda: cfg.get("browser_mode", "off"), None) if cfg else None
    if site._get("browser_manager") is not None:
        rows.append(_row("Browser Automation",
                         str(bmode or "off").capitalize(), "info"))
    rows.append(_row(
        "Network Access",
        "Private URLs allowed" if os.environ.get("ASTRA_ALLOW_PRIVATE_URLS") == "1"
        else "Private/loopback blocked (SSRF guard)",
        "warn" if os.environ.get("ASTRA_ALLOW_PRIVATE_URLS") == "1" else "ok"))
    return rows


# -- posture ------------------------------------------------------------------

def _findings(site, controls, web3) -> list[dict]:
    """Deterministic rules over real configuration. No score is derived."""
    loop = _bind_is_loopback(site)
    out = []

    def add(fid, sev, title, detail):
        out.append({"id": fid, "severity": sev, "title": title, "detail": detail})
    auth_open = not site.operator_token
    auto = web3.get("available") and web3.get("mode") == "AUTO"
    if auth_open and not loop and auto:
        add("auth_open_exposed_auto", "critical", "Open API exposed with Web3 AUTO mode",
            "ASTRA_TOKEN is unset, the server is not bound to loopback, and Web3 AUTO is on")
    elif auth_open and not loop:
        add("auth_open_exposed", "high", "Open API on a non-loopback address",
            "ASTRA_TOKEN is unset while BIND is not loopback")
    elif auth_open:
        add("auth_open_local", "medium", "Operator token not set",
            "Local-first: anyone on this machine can reach the API")
    if not site.rate_limiter:
        add("no_rate_limit", "medium", "Rate limiting disabled", "")
    if not site._allowed_origins and site.env != "production" and not loop:
        add("cors_reflect", "medium", "CORS reflects any origin",
            "Non-loopback bind without ASTRA_CORS_ORIGINS")
    if auto and not web3.get("limits_configured"):
        add("web3_auto_no_limits", "high", "Web3 AUTO mode without limits",
            "Transactions may be pre-authorized with no per-tx/daily limit")
    return out


def build_status(site) -> dict:
    controls = _controls(site)
    prov = _providers(site)
    tools = _tools(site)
    web3 = _web3(site)
    findings = _findings(site, controls, web3)
    counts = {s: sum(1 for f in findings if f["severity"] == s)
              for s in ("critical", "high", "medium")}
    counts["passed"] = sum(1 for c in controls if c["level"] == "ok")
    em = site._get("emergency")
    emergency = em.status() if em is not None else None
    if emergency and emergency["active"]:
        state, label = "shutdown", "Shutdown active"
    elif counts["critical"]:
        state, label = "at_risk", "At risk"
    elif counts["high"]:
        state, label = "attention", "Attention needed"
    else:
        state, label = "protected", "Protected"
    if state == "shutdown":
        summary = "Agent execution is disabled"
    elif counts["critical"]:
        summary = "%d critical issue(s)" % counts["critical"]
    elif counts["high"] or counts["medium"]:
        summary = "No critical issues · %d high, %d medium" % (counts["high"], counts["medium"])
    else:
        summary = "No issues found"
    auth = next(c for c in controls if c["id"] == "auth")
    rl = next(c for c in controls if c["id"] == "rate_limit")
    hd = next(c for c in controls if c["id"] == "headers")
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "posture": {"state": state, "label": label, "summary": summary,
                    "counts": counts, "findings": findings,
                    "score": None},           # no deterministic scoring model
        "api": {"authentication": auth, "rate_limiting": rl, "security_headers": hd},
        "credentials": {k: prov[k] for k in ("keys", "healthy", "failed", "providers")},
        "controls": controls,
        "providers": prov["rows"], "providers_available": prov["available"],
        "gateway_state": prov.get("gateway_state"),
        "tools": tools,
        "web3": web3, "web3_rows": _web3_rows(web3),
        "runtime_rows": _runtime_rows(site, tools),
        "emergency": emergency,
    }
