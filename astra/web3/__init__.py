"""Astra web3 package — chain/RPC readers, transaction pipeline and keystore.

Read paths: chains + rpc (JSON-RPC with per-chain failover, balances,
blocks, receipts). Write paths live behind the deterministic Transaction
Manager: prepare → authorize (policy) → sign (deterministic, RFC 6979) →
broadcast → confirm, with encrypted key storage and a never-sign-twice rule.
"""