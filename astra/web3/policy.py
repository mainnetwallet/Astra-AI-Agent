"""Deterministic Web3 transaction policy engine.

The policy is pure, stateless and deterministic: given the same (mode,
limits, recipient lists, tx request) it always returns the same decision.
There is deliberately *no* LLM anywhere in this file — the agent may PREPARE
a transaction, but only deterministic code VALIDATES, AUTHORIZES, SIGNS and
BROADCASTS.

Modes:
- CONFIRM (default): every send that passes the limits gates on the human.
- AUTO: pre-authorized sends within limits (sender's explicit choice, set
  only out-of-band — never by the LLM).

Boundaries enforced here ("the LLM must NOT"):
 - change mode:  set_mode() is out of the tool surface; only the operator.
 - expand limits / allowlist: the limit fields are configured OOB, never
   mutated through a tool the model can reach.
 - add recipients/contracts/wallets: allowlist edits are operator-only.
 - bypass policy: every path funnels through this one evaluate().
"""
from __future__ import annotations

from dataclasses import dataclass

from astra.core.exceptions import AstraError


class TransactionPolicyError(AstraError):
    """A send was blocked by the deterministic policy (code transaction_policy)."""

    code = "transaction_policy"


class TransactionRejectedError(AstraError):
    """A prepared transaction was rejected (code transaction_rejected)."""

    code = "transaction_rejected"


class TransactionFailedError(AstraError):
    """A signed/submitted transaction failed on-chain (code transaction_failed)."""

    code = "transaction_failed"


# Built-in fallback set used only when the operator has not configured
# WEB3_CHAIN_IDS — keeps existing (pre-config-wiring) behavior unchanged.
DEFAULT_SUPPORTED_CHAINS = frozenset({1, 8453, 137, 56, 42161, 10, 11155111})


@dataclass(frozen=True)
class PolicyConfig:
    mode: str = "CONFIRM"                # 'CONFIRM' | 'AUTO'
    max_tx_value_wei: int = 0            # 0 = unlimited (system-controlled)
    max_daily_tx_value_wei: int = 0      # 0 = unlimited
    allowed_recipients: frozenset = frozenset()   # lowercase addresses, or empty = open
    allowed_contracts: frozenset = frozenset()    # empty = open (with limits)
    allowed_wallets: frozenset = frozenset()      # from-addresses allowed to send
    max_gas_limit: int = 0               # 0 = unlimited
    allowed_chain_ids: frozenset = frozenset()    # empty = use DEFAULT_SUPPORTED_CHAINS
    daily_spent_key: str = "web3_daily_spent"


def normalize_mode(raw: str, default: str = "CONFIRM") -> str:
    """Safely normalize a WEB3_TRANSACTION_MODE value.

    Case-insensitive; any value other than CONFIRM/AUTO falls back to
    `default` (CONFIRM) rather than silently enabling AUTO.
    """
    m = (raw or "").strip().upper()
    return m if m in ("CONFIRM", "AUTO") else default


def normalize_address(addr: str) -> str:
    """Public wrapper for the address-normalization used throughout the
    policy/allowlist logic (lowercase, no '0x' prefix)."""
    return _norm(addr)


@dataclass(frozen=True)
class TxRequest:
    from_address: str
    to_address: str
    value_wei: int
    chain_id: int = 1
    data_hex: str = ""                   # '0x...' contract payload
    gas_limit: int = 0
    max_fee_per_gas: int = 0             # EIP-1559 base/max
    max_priority_fee_per_gas: int = 0
    nonce: int | None = None
    label: str = ""


def _norm(addr: str) -> str:
    a = (addr or "").strip().lower()
    return a[2:] if a.startswith("0x") else a


class Decision:
    __slots__ = ("verdict", "reason", "mode")

    def __init__(self, verdict: str, reason: str, mode: str):
        self.verdict = verdict           # 'allow' | 'ask' | 'block'
        self.reason = reason
        self.mode = mode

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "reason": self.reason,
                "mode": self.mode}

    def __bool__(self):
        return self.verdict != "block"


class TransactionPolicyEngine:
    def __init__(self, cfg: PolicyConfig | None = None):
        self.cfg = cfg or PolicyConfig()

    # -- operator-only control (never reachable from the LLM tool surface) --
    def set_mode(self, mode: str) -> None:
        m = (mode or "").upper()
        if m not in ("CONFIRM", "AUTO"):
            raise TransactionPolicyError(f"invalid web3 mode: {mode}")
        self.cfg = PolicyConfig(
            mode=m, max_tx_value_wei=self.cfg.max_tx_value_wei,
            max_daily_tx_value_wei=self.cfg.max_daily_tx_value_wei,
            allowed_recipients=self.cfg.allowed_recipients,
            allowed_contracts=self.cfg.allowed_contracts,
            allowed_wallets=self.cfg.allowed_wallets,
            max_gas_limit=self.cfg.max_gas_limit,
            allowed_chain_ids=self.cfg.allowed_chain_ids,
            daily_spent_key=self.cfg.daily_spent_key)

    @property
    def mode(self) -> str:
        return self.cfg.mode

    def describe(self) -> dict:
        return {"mode": self.cfg.mode,
                "tx_limit_wei": self.cfg.max_tx_value_wei,
                "daily_limit_wei": self.cfg.max_daily_tx_value_wei,
                "recipients_allowed": sorted(self.cfg.allowed_recipients),
                "contracts_allowed": sorted(self.cfg.allowed_contracts),
                "wallets_allowed": sorted(self.cfg.allowed_wallets),
                "gas_limit_max": self.cfg.max_gas_limit,
                "chains_allowed": sorted(self.cfg.allowed_chain_ids
                                         or DEFAULT_SUPPORTED_CHAINS)}

    # -- the one gate every send must pass -----------------------------------
    def evaluate(self, req: TxRequest,
                 spent_today_wei: int = 0) -> Decision:
        """Deterministic verdict: block / ask / allow."""
        cfg = self.cfg
        mode = cfg.mode

        def block(reason: str) -> Decision:
            return Decision("block", reason, mode)

        def allow(ask: bool) -> Decision:
            return Decision("ask" if ask else "allow",
                            "within policy limits; confirm" if ask
                            else "authorized (AUTO)", mode)

        if cfg.allowed_wallets and _norm(req.from_address) not in cfg.allowed_wallets:
            return block("sender wallet is not allowlisted")
        if cfg.allowed_recipients and _norm(req.to_address) not in cfg.allowed_recipients:
            return block("recipient is not allowlisted")
        if not req.to_address or len(_norm(req.to_address)) != 40:
            return block("malformed recipient address")
        if req.value_wei < 0:
            return block("negative value not allowed")
        if cfg.max_tx_value_wei and req.value_wei > cfg.max_tx_value_wei:
            return block("value exceeds per-transaction limit")
        if cfg.max_daily_tx_value_wei:
            if (spent_today_wei + req.value_wei) > cfg.max_daily_tx_value_wei:
                return block("value would exceed daily send limit")
        if cfg.max_gas_limit and req.gas_limit > cfg.max_gas_limit:
            return block("gas limit exceeds policy maximum")
        if cfg.allowed_contracts and req.data_hex:
            if _norm(req.to_address) not in cfg.allowed_contracts:
                return block("contract call target not allowlisted")
        allowed_chains = cfg.allowed_chain_ids or DEFAULT_SUPPORTED_CHAINS
        if req.chain_id not in allowed_chains:
            return block("chain id not supported")
        return allow(ask=(mode != "AUTO"))