"""Central configuration for Astra.

Precedence (high → low): environment variable > config.json > .env file >
runtime default. Both `PORT` and `ASTRA_PORT` are respected. Secrets are never
written into the DB or logs; the user puts API keys in environment variables
or in `config.json` (gitignored).
"""
from __future__ import annotations

import json
import os


def _load_dotenv(path: str) -> dict:
    env: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


class Config:
    """Read a config value from anywhere, in order: env var, config.json,
    .env file, then the caller's default."""

    def __init__(self, path: str | None = None, prefix: str = "ASTRA_"):
        self.prefix = prefix
        self._json: dict = {}
        self._dotenv: dict = {}
        self._runtime: dict = {}

        # .env lives next to the project root; config.json too
        base = path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "config.json")
        if base and not path:
            dotenv = os.path.join(os.path.dirname(base), ".env")
            self._dotenv = _load_dotenv(dotenv)
        elif path and os.path.isfile(path):
            # explicit config file
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._json = json.load(f) or {}
            except (OSError, ValueError):
                self._json = {}
        elif os.path.isfile(base):
            try:
                with open(base, "r", encoding="utf-8") as f:
                    self._json = json.load(f) or {}
            except (OSError, ValueError):
                self._json = {}

    # -- lookup -------------------------------------------------------------
    def _candidates(self, key: str) -> list[str]:
        return [key, key.upper(), key.lower(), self.prefix + key,
                self.prefix + key.upper()]

    def get(self, key: str, default=None):
        for name in self._candidates(key):
            if name in os.environ:
                v = os.environ[name]
                return v if v != "" else default
        for name in self._candidates(key):
            if name in self._json:
                return self._json[name]
        for name in self._candidates(key):
            if name in self._dotenv:
                v = self._dotenv[name]
                return v if v != "" else default
        for name in self._candidates(key):
            if name in self._runtime:
                return self._runtime[name]
        return default

    def set(self, key: str, value) -> None:
        """In-memory runtime override (used by Settings UI/tests)."""
        self._runtime[key] = value

    def getint(self, key: str, default: int = 0) -> int:
        v = self.get(key)
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def getbool(self, key: str, default: bool = False) -> bool:
        v = self.get(key)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on") if v is not None else default

    def getlist(self, key: str, default: list | None = None) -> list:
        v = self.get(key)
        if v is None:
            return list(default or [])
        if isinstance(v, list):
            return v
        return [x.strip() for x in str(v).replace(",", " ").split() if x.strip()]

    def all(self) -> dict:
        """Public (non-secret) config snapshot for GET /api/config."""
        sup = {"port": 8787, "host": "0.0.0.0", "log_level": "info",
               "browser_mode": "off", "ai_provider": "anthropic",
               "ai_model": "", "data_dir": ""}
        out = {}
        for k in sup:
            out[k] = self.get(k, sup[k])
        out["plugins"] = len(self.getlist("ACTIVE_PLUGINS", default=["airdrop"]))
        return out