"""Raw Ethereum transaction construction, serialization and broadcast.

Pure stdlib: minimal RLP encoder (the seven field shapes of a transaction),
EIP-1559 and legacy-EIP-155 signed serialization, keccak tx hashing, and
broadcast via the existing RPC client. Deterministic: serialize_tx(sig, n)
returns identical bytes for identical inputs — the basis for the
"never sign the same transaction twice" guarantee during recovery.
"""
from __future__ import annotations

from .signer import keccak_256
from . import rpc
from astra.core.exceptions import AstraError

ZERO_ADDR = "0x" + "0" * 40


# ── RLP ───────────────────────────────────────────────────────────────────────
def _rlp_item(b) -> bytes:
    if isinstance(b, bool):
        return _rlp_item(b"\x01" if b else b"\x80")
    if isinstance(b, int):
        if b < 0:
            raise ValueError("negative RLP int")
        if b == 0:
            return b"\x80"
        return _rlp_item(b.to_bytes((b.bit_length() + 7) // 8, "big"))
    if isinstance(b, bytes):
        n = len(b)
        if n == 1 and b[0] < 0x80:
            return b
        return _prefix(0x80, 0xB7, n) + b
    if isinstance(b, str):
        return _rlp_item(bytes.fromhex(b[2:]) if b.startswith("0x") else b.encode())
    if isinstance(b, (list, tuple)):
        payload = b"".join(_rlp_item(x) for x in b)
        return _prefix(0xC0, 0xF7, len(payload)) + payload
    raise TypeError(f"unsupported RLP type: {type(b)}")


def _prefix(short_base: int, long_base: int, length: int) -> bytes:
    if length < 56:
        return bytes([short_base + length])
    ln = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([long_base + len(ln)]) + ln


def rlp_encode(item) -> bytes:
    return _rlp_item(item)


def _int_hex(v: int) -> bytes:
    return b"" if v == 0 else v.to_bytes((v.bit_length() + 7) // 8, "big")


# ── transaction build ─────────────────────────────────────────────────────────
def build_unsigned_eip1559(*, chain_id, nonce, max_priority_fee, max_fee,
                           gas_limit, to, value_wei, data_hex="") -> list:
    """RLP list for EIP-1559 signing."""
    return [
        chain_id, nonce, max_priority_fee, max_fee, gas_limit,
        bytes.fromhex(to[2:]) if to and to != ZERO_ADDR else b"",
        value_wei, bytes.fromhex(data_hex[2:]) if data_hex else b"",
    ]


def build_unsigned_legacy(*, nonce, gas_price, gas_limit, to, value_wei,
                          data_hex="") -> list:
    return [nonce, gas_price, gas_limit,
            bytes.fromhex(to[2:]) if to and to != ZERO_ADDR else b"",
            value_wei, bytes.fromhex(data_hex[2:]) if data_hex else b""]


def signing_hash_eip1559(item: list) -> bytes:
    # EIP-1559 signing hash: keccak256(0x02 || rlp(payload))
    return keccak_256(b"\x02" + rlp_encode(item))


def signing_hash_legacy(item: list, chain_id: int) -> bytes:
    # EIP-155: keccak256(rlp(payload ++ [chain_id, 0, 0]))
    return keccak_256(rlp_encode(item + [chain_id, 0, 0]))


# ── signed serialization ──────────────────────────────────────────────────────
def serialize_eip1559(item: list, *, y_parity: int, r: int, s: int) -> bytes:
    body = rlp_encode(item + [y_parity, r, s])
    return b"\x02" + body


def serialize_legacy(item: list, *, chain_id: int, y_parity: int,
                     r: int, s: int) -> bytes:
    v = chain_id * 2 + 35 + y_parity
    body = rlp_encode(item + [v, r, s])
    return body


def tx_hash(raw: bytes) -> str:
    return "0x" + keccak_256(raw).hex()


# ── chain interaction (via astra.web3.rpc failover) ─────────────────────────
def get_nonce(rpc_urls: list[str], address: str) -> int:
    last_err = None
    for url in rpc_urls:
        try:
            out = rpc.call_with_failover({"rpcs": [url]},
                                         "eth_getTransactionCount",
                                         [address, "pending"])
            return int(out[1], 16)
        except Exception as exc:
            last_err = exc
    raise AstraError(f"nonce fetch failed: {last_err}")


def get_gas_price(rpc_urls: list[str]) -> int:
    last_err = None
    for url in rpc_urls:
        try:
            out = rpc.call_with_failover({"rpcs": [url]}, "eth_gasPrice", [])
            return int(out[1], 16)
        except Exception as exc:
            last_err = exc
    raise AstraError(f"gas price fetch failed: {last_err}")


def broadcast(rpc_urls: list[str], raw_hex: str) -> str:
    """Send the raw signed tx; returns tx hash if accepted."""
    last_err = None
    for url in rpc_urls:
        try:
            out = rpc.call_with_failover({"rpcs": [url]},
                                         "eth_sendRawTransaction", [raw_hex])
            return str(out[1])
        except Exception as exc:
            last_err = exc
    raise AstraError(f"broadcast failed on all RPCs: {last_err}")


def receipt(rpc_urls: list[str], tx_hash: str):
    last_err = None
    for url in rpc_urls:
        try:
            out = rpc.call_with_failover({"rpcs": [url]},
                                         "eth_getTransactionReceipt", [tx_hash])
            return out[1]      # None when not mined yet
        except Exception as exc:
            last_err = exc
    # RPCs hiccup: treat as 'not yet mined' rather than hard-fail
    if last_err:
        return None
    return None