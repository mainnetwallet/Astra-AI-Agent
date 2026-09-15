"""Optional LLM chat for Astra via the Anthropic Messages API — stdlib only.

**Optional**: the agent is fully functional without it. Set ANTHROPIC_API_KEY
to upgrade free-form chat into real AI answers; structured commands are always
handled locally by plugins and never leave the machine.
"""
from __future__ import annotations

import json
import os
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

SYSTEM_PROMPT = (
    "You are Astra AI Agent, a compact, plugin-based personal assistant "
    "embedded in a local work manager. The user farms crypto airdrops and "
    "tracks wallets & deadlines. Keep answers SHORT (max 5 lines), use simple "
    "Banglish (Bangla in English letters) when the user writes Banglish. Warn "
    "if something looks like a scam. Never ask for, or accept, a wallet "
    "private key, seed phrase or signature under pressure — those are always "
    "scams. You do not touch their data directly; plugins handle structured "
    "commands. Just answer the question helpfully."
)


class LLMClient:
    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model
        self.available = bool(self.api_key)

    def ask(self, user_message: str) -> str:
        """One-shot completion. Raises on failure so the server can degrade
        gracefully to the rule-based fallback."""
        if not self.available:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        body = json.dumps({
            "model": self.model,
            "max_tokens": 500,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_message}],
        }).encode("utf-8")
        req = urllib.request.Request(
            API_URL, data=body,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            })
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        blocks = data.get("content", [])
        return "".join(b.get("text", "") for b in blocks).strip() or "(no reply)"