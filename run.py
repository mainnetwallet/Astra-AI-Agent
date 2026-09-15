#!/usr/bin/env python3
"""Astra AI Agent launcher.

      python3 run.py                  # port 8787, browser auto-opens
      PORT=9000 python3 run.py        # custom port
      DATA_DIR=/sdcard/astra python3 run.py   # storage location
      ANTHROPIC_API_KEY=sk-... python3 run.py # enable AI Q&A chat
      AI_PROVIDER=anthropic python3 run.py    # (default provider)
      NO_BROWSER=1 python3 run.py     # don't auto-open the browser
      BIND=127.0.0.1 python3 run.py   # LAN only (privacy)
      ASTRA_SCHEDULER=1 python3 run.py# start the scheduler daemon
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser

from astra.bootstrap import build
from astra.store import Store
from astra.web import AstraServer, AGENT_NAME

# Make "./plugins" importable when run from inside the project dir.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    stack = build(with_scheduler=os.environ.get("ASTRA_SCHEDULER") == "1")
    store: Store = stack["store"]
    agent = stack["agent"]
    plugins = stack["plugins"]
    events = stack["events"]

    config = stack["config"]
    port = config.getint("PORT", 8787)
    bind = config.get("BIND", "0.0.0.0")

    httpd = AstraServer((bind, port), store, agent, plugins,
                        stack=stack)
    url = f"http://localhost:{port}/"

    print("=" * 58)
    print(f"  🚀 {AGENT_NAME} running")
    print(f"  👉 Open:  {url}")
    print(f"  📦 Plugins: {', '.join(p.title for p in plugins if p.enabled)}")
    print(f"  🧠 Orchestrator {('ON' if stack['orchestrator'] else 'off')} | "
          f"Tools: {len(stack['registry'].list())} | "
          f"AI: {'configured' if [p for p in stack['router'].providers if p.name != 'offline'] else 'offline'}")
    if stack["scheduler"]:
        sched = stack["scheduler"]
        print(f"  ⏰ Scheduler running ({len(sched.list())} schedule)")
    print("  (Ctrl+C to stop)")
    print("=" * 58)

    if config.get("NO_BROWSER") != "1":
        try:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBye boss! 👋")
    finally:
        for p in plugins:
            try:
                p.shutdown()
            except Exception:
                pass
        if stack.get("scheduler"):
            stack["scheduler"].stop()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())