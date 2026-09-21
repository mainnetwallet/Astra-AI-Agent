"""Web3 read + prepare tool surface.

Skewed deliberately toward safety: everything here is either a read
(token/chain/rpc/wallet/tx-status) or a *prepare* (tx_prepare creates a
transaction whose fate is gated behind the deterministic Web3 transaction
policy). Semantics depend on the operator-set WEB3_TRANSACTION_MODE:

- CONFIRM (default): tx_prepare only ever prepares. It waits at PREPARED
  for an operator to approve out-of-band (via the gated tx-approval
  endpoint); it never signs itself.
- AUTO: tx_prepare may also complete sign+broadcast itself, but only for a
  transaction the deterministic policy has already authorized (wallet,
  chain, recipient, contract, gas, value and daily-limit checks all pass).
  A transaction the policy blocks is never signed regardless of mode.

Either way, `tx_prepare` never exposes key material, and the model never
calls `authorize()` directly — signing always goes through the
Transaction Manager's own internal, deterministic state transitions.
"""
from __future__ import annotations

from . import rpc as rpcmod
from astra.tools.schemas import Tool, Level


def _resolve_manager(ctx):
    mgr = getattr(ctx, "web3_manager", None) or getattr(ctx, "tx_manager", None)
    return mgr


def tool_token_balance(args: dict, ctx) -> dict:
    """ERC-20 balance for an address/token on a supported chain."""
    address = (args.get("address") or "").strip()
    token = (args.get("token") or "").strip()
    network = (args.get("network") or "ethereum").lower()
    if not address or not token:
        return {"ok": False, "error": "address and token required"}
    import astra.web3.chains as C
    cid = C.chain_for(network)
    if not cid:
        return {"ok": False, "error": f"unknown network '{network}'"}
    chain = C.CHAINS[cid]
    rpc_url = chain["rpcs"][0]
    try:
        raw = rpcmod.erc20_balance(rpc_url, token, address)
        return {"ok": True, "address": address, "token": token,
                "chain": cid, "balance_wei": str(raw),
                "balance": str(rpcmod.to_decimal(raw, chain.get("decimals", 18)))}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}


def tool_chain_status(args: dict, ctx) -> dict:
    """Block number + RPC latency for a chain."""
    network = (args.get("network") or "ethereum").lower()
    import astra.web3.chains as C
    cid = C.chain_for(network)
    if not cid:
        return {"ok": False, "error": f"unknown network '{network}'"}
    chain = C.CHAINS[cid]
    import time
    t0 = time.time()
    try:
        block = rpcmod.block_number(chain["rpcs"][0])
        return {"ok": True, "chain": cid, "name": chain["name"],
                "block": block, "latency_ms": int((time.time() - t0) * 1000),
                "symbol": chain["symbol"]}
    except Exception as exc:
        return {"ok": False, "chain": cid, "error": str(exc)[:160]}


def tool_rpc_status(args: dict, ctx) -> dict:
    """Per-RPC reachability for a chain's failover list."""
    network = (args.get("network") or "ethereum").lower()
    import astra.web3.chains as C
    cid = C.chain_for(network)
    if not cid:
        return {"ok": False, "error": f"unknown network '{network}'"}
    chain = C.CHAINS[cid]
    out = []
    for url in chain.get("rpcs", []):
        ok, block = False, None
        try:
            block = rpcmod.block_number(url)
            ok = True
        except Exception:
            pass
        out.append({"url": url, "ok": ok,
                    "block": block if ok else None})
    return {"ok": True, "chain": cid, "rpcs": out}


def tool_tx_prepare(args: dict, ctx) -> dict:
    """PREPARE a transaction. Gated end-to-end by the deterministic web3 policy.

    CONFIRM mode: the returned tx needs operator approval before broadcast;
    the LLM may create it, it may not bypass the gate.
    AUTO mode: when the policy has already authorized this exact send,
    tx_prepare also completes sign+broadcast itself — no further
    confirmation is asked for. A policy-blocked send is still never signed
    in either mode. Returns a control record with the decision verdict.
    """
    mgr = _resolve_manager(ctx)
    if mgr is None:
        # no manager → offer static read-only guidance, never signing
        pol = getattr(ctx, "policy", None)
        if not pol:
            return {"ok": False, "error": "transaction manager not configured"}
        return {"ok": False,
                "error": "transaction manager not wired — cannot prepare",
                "decision": {"verdict": "block", "reason": "manager missing",
                             "mode": pol.mode if hasattr(pol, "mode") else "UNKNOWN"}}
    try:
        from astra.web3.transactions import TxRequest, TransactionPolicyError
        to = (args.get("to") or "").strip()
        value = int(args.get("value_wei") or args.get("value") or 0)
        chain_id = int(args.get("chain_id") or 1)
        if not (to.lower().startswith("0x") and len(to) == 42):
            return {"ok": False, "error": "invalid recipient address"}
        req = TxRequest(
            from_address=(args.get("from") or "default").strip() or "default",
            to_address=to, value_wei=value, chain_id=chain_id,
            data_hex=args.get("data_hex") or "",
            gas_limit=int(args.get("gas_limit") or 0))
        rec = mgr.create(req)
        rec["ok"] = True
        rec["signed"] = False
        # AUTO mode, within policy: `requires_approval` is False, meaning the
        # deterministic policy already authorized this exact send — complete
        # the pipeline (sign + broadcast) here rather than leaving it parked
        # at PREPARED. CONFIRM-mode sends (requires_approval True) are left
        # untouched for the operator to approve/reject out-of-band; the LLM
        # never reaches authorize()/sign_and_broadcast() itself either way.
        if not rec.get("requires_approval") and rec.get("status") == "PREPARED":
            try:
                final = mgr.sign_and_broadcast(rec["tx_id"])
                rec.update(final)
                rec["signed"] = final.get("status") in ("BROADCAST", "CONFIRMED")
            except TransactionPolicyError as exc:
                rec["ok"] = False
                rec["error"] = str(exc)
            except Exception as exc:
                rec["ok"] = False
                rec["error"] = str(exc)[:200]
        return rec
    except TransactionPolicyError as exc:
        return {"ok": False, "error": str(exc),
                "decision": {"verdict": "block"}, "signed": False}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "signed": False}


def tool_tx_status(args: dict, ctx) -> dict:
    """Query the lifecycle status of a prepared transaction."""
    mgr = _resolve_manager(ctx)
    if mgr is None:
        return {"ok": False, "error": "transaction manager not configured"}
    tx_id = (args.get("tx_id") or "").strip()
    if not tx_id:
        return {"ok": False, "error": "tx_id required"}
    try:
        st = mgr.status(tx_id)
        st["ok"] = True
        return st
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}


def register_web3_tools(reg, manager=None) -> int:
    """Register web3 tools. `manager` (TransactionManager) is wired through
    the ToolContext at execution time via ctx.web3_manager."""
    specs = [
        ("token_balance", tool_token_balance,
         "ERC-20 / token balance for an address on a supported network.",
         Level.READ, False),
        ("chain_status", tool_chain_status,
         "Current block number and RPC latency for a network.",
         Level.READ, False),
        ("rpc_status", tool_rpc_status,
         "Reachability of each RPC endpoint in a chain's failover list.",
         Level.READ, False),
        ("tx_prepare", tool_tx_prepare,
         "PREPARE a transaction (value, recipient, chain). Never signs; ",
         Level.FINANCIAL_ACTION, True),
        ("tx_status", tool_tx_status,
         "Check lifecycle status of a prepared transaction by tx_id.",
         Level.READ, False),
    ]
    n = 0
    for name, fn, desc, risk, conf in specs:
        # tx_prepare's own confirm/allow/block verdict is owned end-to-end
        # by the deterministic Web3 transaction policy (see
        # TransactionManager.create() / TransactionPolicyEngine.evaluate()):
        # CONFIRM mode parks it at PREPARED for operator approval, AUTO mode
        # completes sign+broadcast when authorized. The generic
        # FINANCIAL_ACTION ask-prompt in ToolRegistry must defer to that
        # policy rather than double-gating the same send with a second,
        # non-deterministic confirmation step — so only this tool declares
        # a confirmation_delegate. Every other tool here (and every other
        # FINANCIAL_ACTION tool anywhere else) keeps the plain ask-gate.
        delegate = "web3_tx" if name == "tx_prepare" else ""
        reg.register(Tool(
            name=name, fn=fn, description=desc, category="web3",
            risk=risk, requires_confirmation=conf,
            confirmation_delegate=delegate,
            input={
                "type": "object",
                "properties": {
                    "address": {"type": "string"},
                    "token": {"type": "string"},
                    "network": {"type": "string"},
                    "to": {"type": "string"},
                    "from": {"type": "string"},
                    "value": {"type": "integer"},
                    "value_wei": {"type": "integer"},
                    "chain_id": {"type": "integer"},
                    "data_hex": {"type": "string"},
                    "gas_limit": {"type": "integer"},
                    "tx_id": {"type": "string"},
                },
            },
            timeout=30.0, retries=1, retry_backoff_s=1.0,
            idempotent=name in ("token_balance", "chain_status", "rpc_status",
                                "tx_status"),
            strict=False, plugin="core"))
        n += 1
    return n
