"""Web3Agent — balances, tokens, chain/RPC health, transactions (mode-gated).

Transaction execution is NOT done here: this agent routes the *work*; the
actual signing/broadcast goes through the Web3 Transaction Manager and its
deterministic policy (AUTO/CONFIRM). The agent never owns keys.
"""
from __future__ import annotations

from .base import SpecialistAgent


class Web3Agent(SpecialistAgent):
    name = "web3"
    icon = "⛓️"
    description = ("Wallet balances, ERC20 tokens, chains/RPC health, token "
                   "metadata and transaction preparation — read paths plus "
                   "mode-gated transaction flows.")
    capabilities = ["web3", "balances", "tokens", "rpc", "transactions"]
    tools = ["wallet_balances", "token_balance", "chain_status", "rpc_status",
             "tx_prepare", "tx_status"]
    preferred_task_types = ("web3", "tool_selection")
    required_model_capabilities = ["chat", "tools", "json"]
    priority = 15

    def score(self, goal: str, task_type: str = "") -> int:
        import re
        low = goal.lower()
        if re.search(r"wallet|balance|token|chain|send .*eth|contract|"
                     r"transaction|stake|airdrop claim|gas|nonce", low):
            return 25
        return 0

    def route_hint(self, goal: str) -> dict:
        return {"task_type": "web3", "required_capabilities": ["chat", "tools", "json"]}