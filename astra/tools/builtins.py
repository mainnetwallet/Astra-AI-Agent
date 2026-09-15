"""Built-in tools for Astra.

Every tool follows the signature `fn(args, ctx) -> result_dict`. These cover
memory, task management, research, files and workspace operations — all
offline-first, no secrets stored, confirmation-gated where needed.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime

from astra.core.exceptions import AstraError, ValidationError
from astra.core.permissions import Level
from astra.tools.schemas import Tool


# ── memory tools ────────────────────────────────────────────────────────────────

def remember(args: dict, ctx=None) -> dict:
    """Store a memory for later recall. Supports importance / layer via the
    memory system when one is wired; falls back to a direct insert."""
    content = args.get("content", "").strip()
    if not content:
        raise ValidationError("content required")
    category = args.get("category", "note")
    tags = args.get("tags", "")
    layer = args.get("layer", "long")
    importance = float(args.get("importance", 0.5))
    mem = getattr(ctx, "memory", None) if ctx else None
    if mem is not None:
        r = mem.save(content, category=category, tags=tags, source="tools",
                     layer=layer, importance=importance)
        return {"id": r["id"], "category": category, "layer": r.get("layer"),
                "deduplicated": r.get("deduplicated", False)}
    mid = ctx.store.insert(
        "astra_memories", content=content, category=category,
        tags=tags, source="tools",
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if ctx and ctx.events:
        ctx.events.emit("memory.saved", agent="tools", id=mid, category=category)
    return {"id": mid, "category": category}


def recall(args: dict, ctx=None) -> dict:
    """Search long-term memory by keywords. Uses the memory system's ranked
    recall (importance/confidence weighted) when available."""
    q = args.get("query", "").strip()
    k = int(args.get("k", 5))
    if not q:
        raise ValidationError("query required")
    mem = getattr(ctx, "memory", None) if ctx else None
    if mem is not None:
        rows = mem.search(q, k=k)
        return {"results": rows, "count": len(rows)}
    tokens = [t.lower() for t in q.split() if len(t) >= 2]
    if not tokens:
        return {"results": [], "count": 0}
    # fallback: LIKE-based scoring query (offline-friendly, no embeddings)
    cond = " OR ".join("lower(content) LIKE ?" for _ in tokens)
    LIKE = [f"%{t}%" for t in tokens]
    rows = ctx.store.fetch(
        f"SELECT * FROM astra_memories WHERE {cond} "
        f"ORDER BY created_at DESC LIMIT ?", (*LIKE, k))
    return {"results": rows, "count": len(rows)}


def search_memory(args: dict, ctx=None) -> dict:
    """Alias for recall."""
    return recall(args, ctx)


# ── task tools ──────────────────────────────────────────────────────────────────

def create_task(args: dict, ctx=None) -> dict:
    if not ctx or not ctx.tasks:
        raise AstraError("task engine not initialised")
    goal = args.get("goal", "").strip()
    if not goal:
        raise ValidationError("goal required")
    t = ctx.tasks.create(
        goal=goal, description=args.get("description", ""),
        type=args.get("type", "tool"),
        priority=int(args.get("priority", 0)),
        max_retries=int(args.get("max_retries", 2)),
        **{k: args[k] for k in ("workflow_run_id",) if k in args})
    return {"task_id": t["id"], "goal": t["goal"]}


def list_tasks(args: dict, ctx=None) -> dict:
    if not ctx or not ctx.tasks:
        return {"tasks": [], "count": 0}
    rows = ctx.tasks.list(
        status=args.get("status"), type=args.get("type"),
        limit=int(args.get("limit", 50)))
    return {"tasks": rows, "count": len(rows)}


# ── research / network tools ───────────────────────────────────────────────────

def search_web(args: dict, ctx=None) -> dict:
    """Best-effort DuckDuckGo HTML search (no API key). Offline-graceful."""
    query = args.get("query", "").strip()
    n = int(args.get("n", 5))
    if not query:
        raise ValidationError("query required")
    url = "https://html.duckduckgo.com/html/"
    data = urllib.parse.urlencode({"q": query}).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AstraSearch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "offline": True, "results": [], "count": 0,
                "query": query, "error": f"network_unavailable: {type(e).__name__}"}
    # parse result links
    results = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href = urllib.parse.unquote(m.group(1))
        title = re.sub(r"<.*?>", "", m.group(2)).strip()
        if href.startswith("/"):
            href = "https://duckduckgo.com" + href
        results.append({"url": href, "title": title})
        if len(results) >= n:
            break
    return {"results": results, "count": len(results), "query": query}


def fetch_url(args: dict, ctx=None) -> dict:
    """Fetch a URL and return title + meta description (quick_lookup)."""
    from astra.research import quick_lookup
    reply = quick_lookup(args.get("url", ""))
    return {"title": reply.data.get("title", ""),
            "desc": reply.data.get("desc", ""),
            "url": reply.data.get("url", ""),
            "text": reply.text}


def wallet_balances(args: dict, ctx=None) -> dict:
    """Fetch native-token balances for wallets known to the airdrop plugin.

    Each wallet is checked against every known EVM RPC in web3.chains.
    A wallet whose chain field is set to 'ETH', 'BASE', etc. is queried on
    that chain specifically; 'TBD' wallets are probed on every EVM chain.
    """
    from astra.web3 import chains, rpc
    airdrop = ctx.plugin("airdrop") if ctx else None
    if not airdrop or not hasattr(airdrop, "list_wallets"):
        return {"wallets": [], "note": "airdrop plugin missing"}
    wallets = airdrop.list_wallets()
    balances = []
    target = args.get("network", "").upper() or None
    for w in wallets:
        chain_id = (w.get("network") or "TBD").upper()
        for cid, chain in chains.CHAINS.items():
            if chain_id not in ("TBD", cid):
                continue
            if target and cid != target:
                continue
            rpc_url = chain["rpcs"][0] if chain.get("rpcs") else None
            if not rpc_url:
                continue
            try:
                raw = rpc.native_balance(rpc_url, w["address"])
                balances.append({"address": w["address"], "label": w.get("label"),
                                 "chain": cid, "symbol": chain["symbol"],
                                 "balance_wei": str(raw),
                                 "balance": rpc.to_decimal(raw, chain.get("decimals", 18)),
                                 "url": rpc_url})
            except Exception as e:
                balances.append({"address": w["address"], "chain": cid,
                                 "error": str(e)[:80]})
    return {"wallets": balances, "count": len(balances)}


# ── file / workspace tools ─────────────────────────────────────────────────────

WORKSPACE = os.environ.get("ASTRA_WORKSPACE",
                           os.path.join(os.getcwd(), "workspace"))

def _safe(rel: str) -> str:
    """Resolve a relative path inside WORKSPACE; reject escapes."""
    root = os.path.abspath(WORKSPACE)
    os.makedirs(root, exist_ok=True)
    path = os.path.abspath(os.path.join(root, rel))
    if not path.startswith(root):
        raise ValidationError("path escapes workspace root")
    return path


def list_files(args: dict, ctx=None) -> dict:
    path = _safe(args.get("path", "."))
    entries = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        entries.append({"name": name,
                        "type": "dir" if os.path.isdir(full) else "file",
                        "size": os.path.getsize(full) if os.path.isfile(full) else 0})
    return {"entries": entries, "count": len(entries),
            "path": os.path.relpath(path, os.path.abspath(WORKSPACE))}


def read_file(args: dict, ctx=None) -> dict:
    path = _safe(args.get("path", ""))
    if not os.path.isfile(path):
        raise ValidationError("file not found")
    size = os.path.getsize(path)
    if size > 200_000:
        raise ValidationError("file too large (>200KB)")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read(200_000)
    return {"content": text, "size": size,
            "path": os.path.relpath(path, os.path.abspath(WORKSPACE))}


def write_file(args: dict, ctx=None) -> dict:
    path = _safe(args.get("path", ""))
    content = args.get("content")
    if content is None:
        raise ValidationError("content required")
    if os.path.exists(path) and not args.get("overwrite", False):
        raise ValidationError("file exists; pass overwrite=True to replace")
    os.makedirs(os.path.dirname(path) or WORKSPACE, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(content))
    return {"path": os.path.relpath(path, os.path.abspath(WORKSPACE)),
            "written": len(str(content))}


def search_files(args: dict, ctx=None) -> dict:
    q = args.get("query", "").strip()
    if not q:
        raise ValidationError("query required")
    root = _safe(args.get("path", "."))
    results = []
    for d, _, files in os.walk(root):
        for fn in files:
            fp = os.path.join(d, fn)
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if q.lower() in line.lower():
                            results.append({"file": os.path.relpath(fp, root),
                                            "line": i, "text": line.rstrip()[:200]})
                            if len(results) >= 100:
                                return {"results": results, "count": len(results),
                                        "truncated": True}
            except Exception:
                continue
    return {"results": results, "count": len(results)}


# ── diagnostics ─────────────────────────────────────────────────────────────────

def get_health(args: dict, ctx=None) -> dict:
    """System health summary for GET /api/health."""
    import astra
    info = {"version": astra.__version__, "plugins": [],
            "database": "ok", "tools": len(ctx.tools._tools) if ctx and hasattr(ctx, "tools") else 0}
    for p in (ctx.plugins if ctx else []):
        status = {"slug": p.slug, "enabled": getattr(p, "enabled", True)}
        hc = {}
        try:
            hc = p.health_check() if hasattr(p, "health_check") else {"ok": True}
        except Exception as e:
            hc = {"ok": False, "error": str(e)}
        status["health"] = hc
        info["plugins"].append(status)
    return info


# ── registry helper ─────────────────────────────────────────────────────────────

BUILTIN_TOOLS = [
    # name, fn, category, risk, requires_confirmation
    ("remember",       remember,       "memory",  Level.READ,            False),
    ("recall",         recall,         "memory",  Level.READ,            False),
    ("search_memory",  search_memory,  "memory",  Level.READ,            False),
    ("create_task",    create_task,    "tasks",   Level.LOW_RISK_WRITE,  False),
    ("list_tasks",     list_tasks,     "tasks",   Level.READ,            False),
    ("search_web",     search_web,     "research", Level.READ,           False),
    ("fetch_url",      fetch_url,      "research", Level.READ,           False),
    ("wallet_balances", wallet_balances,"wallet",  Level.READ,            False),
    ("list_files",     list_files,     "files",   Level.READ,            False),
    ("read_file",      read_file,      "files",   Level.READ,            False),
    ("write_file",     write_file,     "files",   Level.LOW_RISK_WRITE,  True),
    ("search_files",   search_files,   "files",   Level.READ,            False),
    ("get_health",     get_health,     "system",  Level.READ,            False),
]


def register_builtins(reg, plugin_slug: str = "") -> int:
    """Register every built-in tool. Returns count."""
    for name, fn, cat, risk, conf in BUILTIN_TOOLS:
        reg.register(Tool(
            name=name, fn=fn,
            description=(fn.__doc__ or name),
            category=cat, risk=risk,
            requires_confirmation=conf,
            plugin=plugin_slug or "core"))
    return len(BUILTIN_TOOLS)