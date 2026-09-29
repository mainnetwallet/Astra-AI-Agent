"""Execution evidence -> deterministic final answer.

Why this exists
---------------
An execution task ("Check RPC status for Ethereum.") can run REAL tools and
still end with a provider that returns nothing usable (empty, the adapters'
``(no reply)`` placeholder, or protocol-only JSON). The verifier/correction
loop is an internal control mechanism; when it is exhausted the user must get
an answer built from what the tools actually returned, never the empty
provider text and never the verifier's state.

This module is the hard safety net for that:

* ``has_sufficient_evidence``  - do the recorded tool steps genuinely answer
  the request? Runtime lifecycle tools (``runtime_status`` / ``runtime_start``
  / ...) are NOT evidence of anything except the runtime itself, and a live
  Web3/RPC request needs an actual RPC/network query.
* ``summarize_execution``      - a user-facing summary DERIVED from the
  recorded step results (hosts, chain id, block, sync state, HTTP failures
  are parsed out of the results; nothing is hardcoded or invented).
* ``NO_EVIDENCE_TEXT``         - the only text used when there is genuinely
  nothing usable (no valid answer AND no sufficient evidence).

Everything returned here still passes through the normal response boundary
(``ChatPipeline._reply`` -> ``sanitize_final_response``): protocol stripping,
credential redaction and media stripping are applied on top of it. Session
ids, trace ids, tool names and other plumbing never appear in the output.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from astra.ai.agent_tool_loop import (LIFECYCLE_TOOLS, _one_line,
                                       summarize_tool_steps)

# Capabilities for which the runtime itself IS the subject, so lifecycle
# tools are legitimate evidence there ("is the runtime running?").
RUNTIME_SUBJECT_CAPABILITIES = frozenset({"runtime", "system"})

# Dedicated read-only chain tools (astra.web3.tools) that perform a real
# network query.
WEB3_QUERY_TOOLS = frozenset({
    "rpc_status", "chain_status", "token_balance", "tx_status"})

NO_EVIDENCE_TEXT = (
    "I couldn't finish that check: the required action did not produce a "
    "usable result. Please try again.")

_RPC_METHOD_RE = re.compile(r'\\?"method\\?"\s*:\s*\\?"([A-Za-z0-9_]+)')
_URL_RE = re.compile(r'https?://[^\s\'"\\]+')
_HTTP_ERR_RES = (
    re.compile(r'returned error:?\s*(\d{3})', re.I),
    re.compile(r'\bHTTP(?:/\d(?:\.\d)?)?\s+(\d{3})\b', re.I),
    re.compile(r'error code:?\s*(\d{3})', re.I),
    re.compile(r'\bstatus(?: code)?:?\s*(\d{3})\b', re.I))
_RPC_NAMESPACES = ("eth_", "net_", "web3_", "debug_", "trace_", "txpool_",
                   "erigon_", "zks_", "arb_", "bor_")


# ── step accessors ───────────────────────────────────────────────────────────
def _tool_steps(steps) -> list:
    return [s for s in (steps or []) if getattr(s, "action", "") == "tool"]


def _result(step) -> dict:
    res = getattr(step, "result", None)
    return res if isinstance(res, dict) else {}


def _executed(step) -> bool:
    """The tool actually RAN (even if the command it ran failed). A tool the
    registry refused (unknown / blocked / awaiting approval) is not evidence."""
    if getattr(step, "ok", False):
        return True
    res = _result(step)
    return any(k in res for k in ("exit_code", "stdout", "stderr"))


def _capability(execution) -> str:
    return str(getattr(execution, "capability", "") or "").strip().lower()


def _lifecycle_counts(execution) -> bool:
    return _capability(execution) in RUNTIME_SUBJECT_CAPABILITIES


def is_lifecycle_step(step) -> bool:
    return getattr(step, "tool", "") in LIFECYCLE_TOOLS


def _command(step) -> str:
    args = getattr(step, "args", None)
    if isinstance(args, dict):
        return str(args.get("command") or args.get("cmd") or "")
    return ""


def _rpc_methods(command: str) -> list[str]:
    return [m for m in _RPC_METHOD_RE.findall(command or "")
            if m.startswith(_RPC_NAMESPACES)]


_NETWORK_QUERY_RE = re.compile(
    r"(--rpc-url|\bcast\s+(?:block|chain|call|balance|tx|gas)|\beth_[a-z]+"
    r"|\bnet_version\b|\bweb3_[a-z]+)", re.I)


def is_rpc_step(step) -> bool:
    """A real RPC/network query: a dedicated chain tool, or a runtime command
    that queries a node (a JSON-RPC method, an HTTP(S) endpoint, or a chain
    client such as `cast ... --rpc-url`). Not tied to any one endpoint."""
    if getattr(step, "tool", "") in WEB3_QUERY_TOOLS:
        return True
    cmd = _command(step)
    return bool(cmd and (_URL_RE.search(cmd) or _NETWORK_QUERY_RE.search(cmd)))


# ── evidence gate ────────────────────────────────────────────────────────────
def evidence_steps(steps, execution=None, *, executed_only: bool = True) -> list:
    """The steps that count as evidence for `execution`: they are not runtime
    lifecycle noise (unless the runtime is the subject) and, by default, the
    tool actually RAN. `executed_only=False` is the looser completion-gate
    view (an attempted action is an "observed tool action"); the answer we
    show a user is always built from executed steps only."""
    keep_lifecycle = _lifecycle_counts(execution)
    return [s for s in _tool_steps(steps)
            if (not executed_only or _executed(s))
            and (keep_lifecycle or not is_lifecycle_step(s))]


def has_sufficient_evidence(steps, execution=None, *,
                            executed_only: bool = True) -> bool:
    """True when the recorded tool steps genuinely answer the request.

    * lifecycle tools alone (runtime_status/runtime_start/...) never do,
      unless the runtime itself is the capability being asked about;
    * a live Web3 request needs an actual RPC/network query (a failed query
      still counts: "the endpoint returned HTTP 525" is a real answer);
    * any other execution task needs at least one real tool step.
    """
    work = evidence_steps(steps, execution, executed_only=executed_only)
    if not work:
        return False
    if _capability(execution) == "web3":
        return any(is_rpc_step(s) for s in work)
    return True


def evidence_snapshot(steps, execution=None) -> dict:
    """`{"tool_execution": {...}}` for the Gateway's completion gate, or `{}`
    when the steps are not sufficient evidence (so the gate stays closed).
    The gate only needs an observed tool action; it is NOT the user-facing
    answer, so an attempted step counts here (the semantic verifier and the
    final invariant still judge/derive the answer from executed steps)."""
    if not has_sufficient_evidence(steps, execution, executed_only=False):
        return {}
    work = evidence_steps(steps, execution, executed_only=False)
    tools: list[str] = []
    for s in work:
        if s.tool and s.tool not in tools:
            tools.append(s.tool)
    return {"tool_execution": {"count": len(work), "tools": tools,
                               "last_status": getattr(work[-1], "status", "")}}


# ── RPC result parsing ───────────────────────────────────────────────────────
@dataclass
class _Host:
    name: str
    ok: bool = False
    facts: dict = field(default_factory=dict)      # label -> text
    failures: list = field(default_factory=list)   # short reasons


def _chain_by_id(chain_id):
    try:
        from astra.web3.chains import CHAINS
        for c in CHAINS.values():
            if int(c.get("chain_id", -1)) == int(chain_id):
                return c
    except Exception:
        pass
    return None


def _chain_by_key(key):
    try:
        from astra.web3.chains import CHAINS
        return CHAINS.get(str(key))
    except Exception:
        return None


def _host_of(url: str) -> str:
    try:
        return urlparse(url).hostname or url
    except Exception:
        return url


def _loads(text: str):
    text = (text or "").strip()
    if not text:
        return None
    for candidate in (text, text.splitlines()[-1] if text.splitlines() else ""):
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _http_status(*texts) -> int:
    for t in texts:
        for rx in _HTTP_ERR_RES:
            m = rx.search(t or "")
            if m:
                return int(m.group(1))
    return 0


def _failure_reason(res: dict, step) -> str:
    stdout, stderr = str(res.get("stdout") or ""), str(res.get("stderr") or "")
    code = _http_status(stderr, stdout, step.error, res.get("error"))
    if code:
        return f"HTTP {code}"
    exit_code = res.get("exit_code")
    if exit_code == 28 or "timed out" in (stderr + stdout).lower():
        return "timed out"
    if exit_code in (6, 7):
        return "connection failed"
    if exit_code not in (None, 0):
        return f"failed (exit code {exit_code})"
    return "failed"


def _rpc_error_text(err) -> str:
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or err
    else:
        msg = err
    return _one_line(msg, 120) or "RPC error"


def _apply_result(host: _Host, method: str, value) -> None:
    """Record one successful RPC method result as a labelled fact."""
    host.ok = True
    if method == "eth_blockNumber":
        host.facts["Latest block"] = str(value)
    elif method in ("net_version", "eth_chainId"):
        try:
            cid = int(str(value), 16) if (
                method == "eth_chainId" or str(value).lower().startswith("0x")
            ) else int(str(value))
            host.facts["_chain_id"] = str(cid)
        except Exception:
            host.facts["Network"] = _one_line(value, 60)
    elif method == "eth_syncing":
        host.facts["Syncing"] = ("false" if value in (False, "false", None)
                                 else "true")
    elif method == "web3_clientVersion":
        host.facts["Client"] = _one_line(value, 80)
    else:
        host.facts[method] = _one_line(
            value if isinstance(value, str) else json.dumps(value, default=str),
            80)


def _parse_command_step(step, hosts: dict) -> bool:
    """Fold one runtime_command RPC step into `hosts`. False when the command
    is not a single-endpoint RPC query we can parse (it is then reported
    through the generic per-step line instead)."""
    cmd = _command(step)
    urls = list(dict.fromkeys(_URL_RE.findall(cmd)))
    methods = _rpc_methods(cmd)
    if len(urls) != 1 or len(set(methods)) != 1:
        return False
    method = methods[0]
    host = hosts.setdefault(_host_of(urls[0]), _Host(_host_of(urls[0])))
    res = _result(step)
    body = _loads(str(res.get("stdout") or ""))
    if isinstance(body, dict) and "result" in body and body.get("error") is None:
        _apply_result(host, method, body["result"])
        return True
    if isinstance(body, dict) and body.get("error") is not None:
        host.failures.append(
            f"returned an RPC error ({_rpc_error_text(body['error'])})")
        return True
    # Not a JSON-RPC body. A gateway/CDN error page (e.g. Cloudflare's
    # "error code: 525") arrives with curl exit 0, so look for an HTTP status
    # in the body/stderr before calling the response merely "unreadable".
    stdout, stderr = str(res.get("stdout") or ""), str(res.get("stderr") or "")
    if (_http_status(stdout, stderr, step.error, res.get("error"))
            or not getattr(step, "ok", False) or res.get("exit_code")):
        host.failures.append(_failure_reason(res, step))
    else:
        host.failures.append("returned an unreadable response")
    return True


def _parse_tool_step(step, hosts: dict, meta: dict) -> bool:
    """Fold a dedicated web3 chain tool result into `hosts`/`meta`."""
    res, tool = _result(step), getattr(step, "tool", "")
    if tool == "rpc_status" and isinstance(res.get("rpcs"), list):
        chain = _chain_by_key(res.get("chain"))
        if chain:
            meta["chain"] = chain
        for row in res["rpcs"]:
            if not isinstance(row, dict):
                continue
            h = hosts.setdefault(_host_of(str(row.get("url") or "")),
                                 _Host(_host_of(str(row.get("url") or ""))))
            if row.get("ok"):
                h.ok = True
                if row.get("block") is not None:
                    h.facts["Latest block"] = str(row["block"])
            else:
                h.failures.append("did not respond")
        return True
    if tool == "chain_status":
        chain = _chain_by_key(res.get("chain"))
        if chain:
            meta["chain"] = chain
        h = hosts.setdefault("primary RPC endpoint",
                             _Host("primary RPC endpoint"))
        if getattr(step, "ok", False) and res.get("block") is not None:
            h.ok = True
            h.facts["Latest block"] = str(res["block"])
        else:
            h.failures.append(_one_line(res.get("error") or "did not respond",
                                        120))
        return True
    return False


def _failure_line(host: str, reason: str) -> str:
    if reason.startswith(("HTTP ", "returned ")):
        return f"{host} {'returned ' + reason if reason.startswith('HTTP ') else reason} during the check."
    if reason == "timed out":
        return f"{host} timed out during the check."
    if reason == "connection failed":
        return f"{host} could not be reached during the check."
    return f"{host} {reason} during the check."


def _rpc_summary(steps) -> str:
    hosts: dict[str, _Host] = {}
    meta: dict = {}
    for s in _tool_steps(steps):
        if not _executed(s) or is_lifecycle_step(s):
            continue
        if s.tool in ("rpc_status", "chain_status"):
            _parse_tool_step(s, hosts, meta)
        elif s.tool == "runtime_command":
            _parse_command_step(s, hosts)
    if not hosts:
        return ""

    # Network name/id: from a reported chain id (derived, never assumed).
    chain = meta.get("chain")
    chain_id = next((h.facts["_chain_id"] for h in hosts.values()
                     if "_chain_id" in h.facts), "")
    if chain is None and chain_id:
        chain = _chain_by_id(chain_id)
    network = ""
    if chain:
        network = f"{chain['name']} mainnet (chain ID {chain['chain_id']})"
    elif chain_id:
        network = f"chain ID {chain_id}"

    good = [h for h in hosts.values() if h.ok]
    bad = [h for h in hosts.values() if h.failures]
    name = chain["name"] if chain else ""
    head = f"{name + ' ' if name else ''}RPC check completed."
    if not good:
        head = (f"{name + ' ' if name else ''}RPC check completed, but no "
                "endpoint responded successfully.")
    lines = [head]

    def facts_of(h: _Host) -> list[str]:
        out = []
        if network:
            out.append(f"Network: {network}")
        for label in ("Latest block", "Syncing", "Client"):
            if label in h.facts:
                out.append(f"{label}: {h.facts[label]}")
        for k, v in h.facts.items():
            if k not in ("Latest block", "Syncing", "Client", "_chain_id",
                         "Network"):
                out.append(f"{k}: {v}")
        return out

    if good:
        lines.append("")
        if len(good) == 1:
            lines.append(f"- {good[0].name}: responsive")
            lines += [f"- {f}" for f in facts_of(good[0])]
        else:
            for h in good:
                lines.append(f"- {h.name}: responsive")
                lines += [f"  - {f}" for f in facts_of(h)]
    if bad:
        lines.append("")
        for h in bad:
            lines.append(_failure_line(h.name, h.failures[0]))
    return "\n".join(lines).strip()


# ── public summary ───────────────────────────────────────────────────────────
def summarize_execution(steps, execution=None) -> str:
    """Deterministic, user-safe answer derived ONLY from recorded tool
    results. RPC-aware when the results are RPC queries; otherwise the
    generic per-step summary of the steps that count as evidence. Returns ""
    when there is nothing that counts as evidence."""
    if not has_sufficient_evidence(steps, execution):
        return ""
    rpc = _rpc_summary(steps)
    if rpc:
        return rpc
    return summarize_tool_steps(evidence_steps(steps, execution))
