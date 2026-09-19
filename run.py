#!/usr/bin/env python3
"""Astra AI Agent launcher.

      python3 run.py                  # port 8787, browser auto-opens
      PORT=9000 python3 run.py        # custom port
      DATA_DIR=/sdcard/astra python3 run.py   # storage location
      ANTHROPIC_API_KEY=sk-... python3 run.py # enable AI Q&A chat
      AI_PROVIDER=anthropic python3 run.py    # (default provider)
      NO_BROWSER=1 python3 run.py     # don't auto-open the browser
      BIND=127.0.0.1 python3 run.py   # default: loopback only (private)
      BIND=0.0.0.0  python3 run.py    # LAN access (set ASTRA_TOKEN first!)
      ASTRA_TOKEN=... python3 run.py  # protect every /api/* with a token
      ASTRA_SCHEDULER=1 python3 run.py# start the scheduler daemon

Security defaults: binds 127.0.0.1 (safe local), open API only when
ASTRA_TOKEN is unset, strict CORS, per-IP rate limit, body cap.
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
    events = stack["events"]

    config = stack["config"]
    port = config.getint("PORT", 8787)
    # secure default: loopback only; operator opts into LAN exposure
    bind = config.get("BIND", "127.0.0.1")
    token = config.get("ASTRA_TOKEN") or ""

    httpd = AstraServer((bind, port), store, agent,
                        stack=stack)
    url = f"http://localhost:{port}/"

    print("=" * 58)
    print(f"  🚀 {AGENT_NAME} running")
    print(f"  👉 Open:  {url}")
    print(f"  🔒 Bind: {bind} | "
          f"API auth: {'token protected' if token else 'OPEN (set ASTRA_TOKEN)'}")
    print("  📦 Plugins: none (plugins/ is empty — see plugins/README.md)")
    print(f"  🧠 Orchestrator {('ON' if stack['orchestrator'] else 'off')} | "
          f"Tools: {len(stack['registry'].list())} | "
          f"AI: {'configured' if stack['router'].providers else 'not configured'}")
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
        if stack.get("scheduler"):
            stack["scheduler"].stop()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())