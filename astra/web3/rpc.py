"""Read-only JSON-RPC client for EVM chains (stdlib urllib, failover).

Reimplements the smallest useful subset of Ethereum JSON-RPC behavior needed
by Astra: native balance, ERC-20 balance, block number and a contract check.
No signing, no transaction submission.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

from astra.core.exceptions import AstraError, NetworkError, TimeoutError

TIMEOUT = 8


def json_rpc(rpc_url: str, method: str, params: list, timeout: int = TIMEOUT):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode("utf-8")
    req = urllib.request.Request(rpc_url, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": "AstraAI/1.0 (web3 reader)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except ValueError as e:
        raise AstraError(f"rpc bad json from {rpc_url}") from e
    except urllib.error.URLError as e:
        raise NetworkError(f"rpc network error: {getattr(e, 'reason', e)}") from e
    except socket.timeout as e:
        raise TimeoutError(f"rpc timeout: {rpc_url}") from e
    except TimeoutError as e:
        raise e
    if "error" in data and data["error"]:
        raise AstraError(f"rpc error: {data['error']}")
    return data.get("result")


def call_with_failover(chain: dict, method: str, params: list) -> tuple[str, object]:
    """Try each RPC in the chain's failover list; return (url, result)."""
    errs = []
    for url in chain.get("rpcs", []):
        try:
            return url, json_rpc(url, method, params)
        except (NetworkError, TimeoutError, AstraError) as e:
            errs.append(f"{url}: {type(e).__name__}")
    raise NetworkError(f"all RPCs failed for {chain.get('name')}: " + "; ".join(errs))


def native_balance(rpc_url: str, address: str) -> int:
    _, wei = call_with_failover({"rpcs": [rpc_url]}, "eth_getBalance",
                                [address, "latest"])
    return int(wei or "0", 16)


def erc20_balance(rpc_url: str, token: str, address: str) -> int:
    # balanceOf(address) -> selector 0x70a08231
    data = "0x70a08231" + address[2:].lower().rjust(64, "0")
    _, result = call_with_failover({"rpcs": [rpc_url]}, "eth_call",
                                   [{"to": token, "data": data}, "latest"])
    return int(result or "0x0", 16)


def block_number(rpc_url: str) -> int:
    _, hx = call_with_failover({"rpcs": [rpc_url]}, "eth_blockNumber", [])
    return int(hx or "0x0", 16)


def is_contract(rpc_url: str, address: str) -> bool:
    _, code = call_with_failover({"rpcs": [rpc_url]}, "eth_getCode", [address, "latest"])
    return bool(code) and code not in ("0x", "0x0")


def to_decimal(wei: int, decimals: int = 18) -> str:
    """Format an integer wei value to a decimal string without float error."""
    if decimals <= 0:
        return str(wei)
    whole, frac = divmod(abs(wei), 10 ** decimals)
    sign = "-" if wei < 0 else ""
    return f"{sign}{whole}.{frac:0{decimals}d}".rstrip("0").rstrip(".")


def get_transaction(rpc_url: str, tx_hash: str) -> dict:
    _, tx = call_with_failover({"rpcs": [rpc_url]}, "eth_getTransactionByHash", [tx_hash])
    return tx if isinstance(tx, dict) else {"hash": tx_hash, "unconfirmed": True}