"""Pure-standard-library Ethereum signing.

No third-party crypto: secp256k1 arithmetic, RFC 6979 deterministic nonces,
Keccak-256 (implemented here — note the finalizer differs from NIST SHA3),
EIP-155 chain-id signing and payment-address derivation. Every helper is
deterministic and unit-tested against known vectors where feasible.

Secrets stay in local variables (never logged): callers receive signatures
and addresses, not key material.
"""
from __future__ import annotations

import hashlib
import hmac

from astra.core.exceptions import AstraError


class SignerError(AstraError):
    pass


# ── secp256k1 parameters ──────────────────────────────────────────────────────
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


class _ECPoint:
    __slots__ = ("x", "y")

    def __init__(self, x=None, y=None):
        self.x, self.y = x, y

    def is_infinity(self):
        return self.x is None

    def copy(self):
        return _ECPoint(self.x, self.y)


def _inv(a: int, m: int) -> int:
    """Modular inverse via extended Euclid."""
    if a == 0:
        raise ZeroDivisionError("0 has no modular inverse")
    lm, hm = 1, 0
    low, high = a % m, m
    while low > 1:
        r = high // low
        nm, new = hm - lm * r, high - low * r
        lm, low, hm, high = nm, new, lm, low
    return lm % m


def _pt_add(p: _ECPoint, q: _ECPoint) -> _ECPoint:
    if p.is_infinity():
        return q.copy()
    if q.is_infinity():
        return p.copy()
    if p.x == q.x:
        if (p.y + q.y) % P == 0:
            return _ECPoint()
        lam = (3 * p.x * p.x) * _inv(2 * p.y, P) % P
    else:
        lam = (q.y - p.y) * _inv(q.x - p.x, P) % P
    x3 = (lam * lam - p.x - q.x) % P
    y3 = (lam * (p.x - x3) - p.y) % P
    return _ECPoint(x3, y3)


def _pt_mul(k: int, p: _ECPoint) -> _ECPoint:
    k = k % N
    r = _ECPoint()
    addend = p.copy()
    while k:
        if k & 1:
            r = _pt_add(r, addend)
        addend = _pt_add(addend, addend)
        k >>= 1
    return r


G = _ECPoint(GX, GY)


def _bytes_to_int(b: bytes) -> int:
    return int.from_bytes(b, "big")


def _int_to_bytes32(i: int) -> bytes:
    return i.to_bytes(32, "big")


# ── Keccak-256 (sponge) ────────────────────────────────────────────────────────
_ROUND_CONST = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
# rho rotation offsets, indexed RHO[x + 5*y] (column-major, y outer) per the
# Keccak reference arrangement; RHO[0] == 0 keeps the (0,0) lane unmoved.
_RHO = (
    0,  1, 62, 28, 27,
    36, 44,  6, 55, 20,
    3, 10, 43, 25, 39,
    41, 45, 15, 21,  8,
    18,  2, 61, 56, 14,
)


def keccak_256(data: bytes) -> bytes:
    """Keccak-256 digest (Ethereum-correct: pad10*1 with domain 0x01).

    Note the finalizer differs from NIST SHA3 (0x06): this is the variant
    Ethereum uses, and it must match on-chain behaviour exactly.
    """
    rate = 136
    # pad10*1: append 0x01, enough 0x00 so the final 0x80 lands at the end of
    # a rate-sized block. Padding is never empty (Keccak absorbs at least one
    # 0x01 marker).
    n_blocks = (len(data) + 2 + rate - 1) // rate   # 2 = 0x01 marker + 0x80
    block = bytearray(data)
    block.append(0x01)
    block.extend(b"\x00" * (n_blocks * rate - len(block) - 1))
    block.append(0x80)
    state = [0] * 25
    for off in range(0, len(block), rate):
        chunk = block[off:off + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(chunk[i * 8:(i + 1) * 8], "little")
        state = _keccak_f(state)
    # squeeze: 32 bytes fit inside the first rate block (lanes 0..3)
    return b"".join(lane.to_bytes(8, "little") for lane in state[:4])


def _keccak_f(state: list[int]) -> list[int]:
    A = [[state[x + 5 * y] for y in range(5)] for x in range(5)]
    for rc in _ROUND_CONST:
        # theta
        C = [A[x][0] ^ A[x][1] ^ A[x][2] ^ A[x][3] ^ A[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rotl64(C[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                A[x][y] ^= D[x]
        # rho + pi
        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                r = _RHO[x + 5 * y]
                B[y][(2 * x + 3 * y) % 5] = _rotl64(A[x][y], r)
        # chi
        for x in range(5):
            for y in range(5):
                cx, cy = x, y
                A[cx][cy] = B[cx][cy] ^ (
                    (~B[(cx + 1) % 5][cy]) & B[(cx + 2) % 5][cy])
        # iota
        A[0][0] ^= rc
    # lane index is x + 5*y (y outer): the same order absorption/squeeze use
    return [A[x][y] for y in range(5) for x in range(5)]


def _rotl64(v: int, n: int) -> int:
    return ((v << n) | (v >> (64 - n))) & 0xFFFFFFFFFFFFFFFF if n else v


# ── keys & addresses ──────────────────────────────────────────────────────────
def private_to_public(priv: int) -> tuple[int, int]:
    if priv <= 0 or priv >= N:
        raise SignerError("invalid private key")
    q = _pt_mul(priv, G)
    return q.x, q.y


def private_to_address(priv: int) -> str:
    """Ethereum payment address from a private key (Keccak last-20)."""
    x, y = private_to_public(priv)
    pub = b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")
    return "0x" + keccak_256(pub)[-20:].hex()


def generate_private_key() -> int:
    """Cryptographically random scalar in [1, n)."""
    import os
    while True:
        b = os.urandom(32)
        k = _bytes_to_int(b)
        if 1 <= k < N:
            return k


# ── RFC 6979 deterministic nonce ──────────────────────────────────────────────
def _nonce_rfc6979(priv: int, msg_hash: bytes) -> int:
    x = _int_to_bytes32(priv)
    h1 = msg_hash
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = _bytes_to_int(v)
        if 1 <= cand < N:
            return cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


# ── signing ───────────────────────────────────────────────────────────────────
def _low_s(s: int) -> int:
    return s if s <= N // 2 else N - s


def sign(priv: int, msg_hash: bytes, chain_id: int = 0) -> dict:
    """Deterministic ECDSA signature with recovery id.

    Returns {"r","s","v","recovery_id"} — v is EIP-155 when chain_id > 0.
    """
    z = _bytes_to_int(msg_hash)
    k = _nonce_rfc6979(priv, msg_hash)
    R = _pt_mul(k, G)
    if R.is_infinity():
        raise SignerError("degenerate signing point")   # impossibly rare
    r = R.x % N
    if r == 0:
        raise SignerError("degenerate signature r")
    s = _inv(k, N) * (z + r * priv) % N
    if s == 0:
        raise SignerError("degenerate signature s")
    recovery = (R.y & 1) | (0 if R.x < N else 2)
    if chain_id:
        v = chain_id * 2 + 35 + recovery
    else:
        v = 27 + recovery
    return {"r": hex(r), "s": hex(_low_s(s)),
            "v": v, "recovery_id": recovery, "y_parity": (R.y & 1)}


def derive_private_key_bip39(mnemonic: str, password: str = "") -> bytes:
    """BIP-39 seed from mnemonic phrase (pure stdlib PBKDF2)."""
    phrase = " ".join(mnemonic.split()).encode("utf-8")
    salt = ("mnemonic" + password).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha512", phrase, salt, 2048, dklen=64)


def private_key_bytes(priv: int) -> bytes:
    return _int_to_bytes32(priv)