#!/usr/bin/env python3
"""Astra AI Agent launcher.

      python3 run.py                  # port 8787, browser auto-opens
      PORT=9000 python3 run.py        # custom port
      DATA_DIR=/sdcard/astra python3 run.py   # storage location
      ANTHROPIC_API_KEY=sk-... python3 run.py # enable AI Q&A chat
      NO_BROWSER=1 python3 run.py     # don't auto-open the browser
      BIND=127.0.0.1 python3 run.py   # LAN only (privacy)
"""
from __future__ import annotations

import importlib
import os
import sys
import threading
import webbrowser

from astra.core import Registry
from astra.store import Store
from astra.agent import Agent
from astra.llm import LLMClient
from astra.web import AstraServer, AGENT_NAME

# Plugins live in ./plugins; register them here by module name. Adding a
# future feature = add one line. (Order is the module's own `order` attr.)
PLUGIN_MODULES = ["plugins.airdrop"]

# Make "./plugins" importable when run from inside the project dir.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def build_registry() -> Registry:
    """Each plugin module exposes `PLUGIN` = its Plugin class. Discover them
    by walking PLUGIN_MODULES so adding a future domain is one line."""
    reg = Registry()
    for mod_name in PLUGIN_MODULES:
        mod = importlib.import_module(mod_name)
        plugin_cls = getattr(mod, "Plugin", None) or getattr(mod, "PLUGIN", None)
        if plugin_cls is None:
            # convention: first public Plugin subclass defined in that module
            from astra.core import Plugin as PluginBase
            plugin_cls = next(v for v in vars(mod).values()
                              if isinstance(v, type)
                              and issubclass(v, PluginBase)
                              and v.__module__ == mod.__name__)
        reg.add(plugin_cls)
    return reg


def _default_db_path() -> str:
    data_dir = os.environ.get("DATA_DIR", os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data"))
    return os.path.join(data_dir, "astra.db")


def main() -> int:
    port = int(os.environ.get("PORT", "8787"))
    bind = os.environ.get("BIND", "0.0.0.0")
    db_path = os.environ.get("DATABASE", _default_db_path())
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    store = Store(db_path)
    plugins = build_registry().load(store)

    llm = LLMClient()
    if llm.available:
        print(f"[Astra AI Agent] LLM chat ENABLED (Anthropic API key found)")
    else:
        print("[Astra AI Agent] LLM chat disabled — local rule-based only. "
              "Set ANTHROPIC_API_KEY to enable AI Q&A.")

    agent = Agent(plugins, llm=llm.ask if llm.available else None)
    httpd = AstraServer((bind, port), store, agent, plugins)

    url = f"http://localhost:{port}/"
    print("=" * 56)
    print(f"  🚀 {AGENT_NAME} running")
    print(f"  👉 Open:  {url}")
    print(f"  📦 Plugins: {', '.join(p.title for p in plugins)}")
    print("  (Ctrl+C to stop)")
    print("=" * 56)

    if os.environ.get("NO_BROWSER") != "1":
        try:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBye boss! 👋")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())