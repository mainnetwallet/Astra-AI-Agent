"""Chain definitions: EVM chains with failover public RPC lists.

Read-only helpers only (balance, block, smart-contract check). Nothing here
can move funds or read secrets — signing/private keys are never touched.
New chains are added by extending this dict.
"""
from __future__ import annotations

CHAINS = {
    "eth": {"name": "Ethereum", "symbol": "ETH", "chain_id": 1, "decimals": 18,
            "rpcs": ["https://cloudflare-eth.com", "https://ethereum-rpc.publicnode.com",
                     "https://rpc.ankr.com/eth"]},
    "base": {"name": "Base", "symbol": "ETH", "chain_id": 8453, "decimals": 18,
             "rpcs": ["https://mainnet.base.org", "https://base-rpc.publicnode.com"]},
    "arbitrum": {"name": "Arbitrum", "symbol": "ETH", "chain_id": 42161, "decimals": 18,
                 "rpcs": ["https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com"]},
    "optimism": {"name": "Optimism", "symbol": "ETH", "chain_id": 10, "decimals": 18,
                 "rpcs": ["https://mainnet.optimism.io", "https://optimism-rpc.publicnode.com"]},
    "polygon": {"name": "Polygon", "symbol": "POL", "chain_id": 137, "decimals": 18,
                "rpcs": ["https://polygon-rpc.com", "https://polygon-bor-rpc.publicnode.com"]},
    "bnb": {"name": "BNB Chain", "symbol": "BNB", "chain_id": 56, "decimals": 18,
            "rpcs": ["https://bsc-dataseed.bnbchain.org", "https://binance.llamarpc.com"]},
}

ALIAS = {
    "ethereum": "eth", "ether": "eth", "base": "base", "arb": "arbitrum",
    "arbitrum": "arbitrum", "op": "optimism", "optimism": "optimism",
    "polygon": "polygon", "matic": "polygon", "pol": "polygon",
    "bnb": "bnb", "bsc": "bnb", "binance": "bnb",
}


def chain_for(label: str) -> str | None:
    """Map a wallet/airdrop network label ('ETH', 'BSC', 'matic', …) to a
    chain id slug, case-insensitively."""
    if not label:
        return None
    key = label.strip().lower()
    return ALIAS.get(key) or (key if key in CHAINS else None)


def list_chains() -> dict:
    return {cid: {"name": c["name"], "symbol": c["symbol"], "chain_id": c["chain_id"],
                  "decimals": c["decimals"], "rpcs": len(c["rpcs"])}
            for cid, c in CHAINS.items()}