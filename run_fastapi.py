#!/usr/bin/env python3
"""Astra AI Agent launcher — FastAPI edition.

      python3 run_fastapi.py                  # port 8787, browser auto-opens
      PORT=9000 python3 run_fastapi.py         # custom port
      DATA_DIR=/sdcard/astra python3 run_fastapi.py
      ANTHROPIC_API_KEY=sk-... python3 run_fastapi.py
      NO_BROWSER=1 python3 run_fastapi.py
      BIND=127.0.0.1 python3 run_fastapi.py    # default: loopback only
      BIND=0.0.0.0  python3 run_fastapi.py     # LAN access (set ASTRA_TOKEN first!)
      ASTRA_TOKEN=... python3 run_fastapi.py
      ASTRA_SCHEDULER=1 python3 run_fastapi.py

Same security defaults and route surface as run.py — only the HTTP layer
(now FastAPI + uvicorn instead of stdlib http.server) differs.

Requires:  pip install fastapi "uvicorn[standard]" python-multipart
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from astra.bootstrap import build
from astra.web_fastapi import make_app, AGENT_NAME


def main() -> int:
    try:
        import uvicorn
    except ImportError:
        print("FastAPI/uvicorn not installed. Run:")
        print("  pip install fastapi \"uvicorn[standard]\" python-multipart")
        return 1

    stack = build(with_scheduler=os.environ.get("ASTRA_SCHEDULER") == "1")
    store = stack["store"]
    agent = stack["agent"]
    config = stack["config"]

    port = config.getint("PORT", 8787)
    bind = config.get("BIND", "127.0.0.1")
    token = config.get("ASTRA_TOKEN") or ""

    app = make_app(store, agent, stack=stack)
    url = f"http://localhost:{port}/"

    print("=" * 58)
    print(f"  🚀 {AGENT_NAME} running (FastAPI)")
    print(f"  👉 Open:  {url}")
    print(f"  📖 Docs:  {url}api/docs")
    print(f"  🔒 Bind: {bind} | "
          f"API auth: {'token protected' if token else 'OPEN (set ASTRA_TOKEN)'}")
    gw = stack["router"].gateway
    print(f"  🧠 Chat pipeline ON (Gateway "
          f"{'ready' if gw is not None and gw.is_usable() else 'not configured — answers unverified'}) | "
          f"Tools: {len(stack['registry'].list())} | "
          f"AI: {'configured' if stack['router'].providers else 'not configured'}")
    if stack.get("scheduler"):
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
        uvicorn.run(app, host=bind, port=port, log_level="warning")
    except KeyboardInterrupt:
        print("\nBye boss! 👋")
    finally:
        if stack.get("scheduler"):
            stack["scheduler"].stop()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
