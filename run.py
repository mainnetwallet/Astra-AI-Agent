#!/usr/bin/env python3
"""Astra AI Agent launcher — FastAPI/uvicorn.

      python3 run.py                  # port 8787, browser auto-opens
      PORT=9000 python3 run.py        # custom port
      DATA_DIR=/sdcard/astra python3 run.py   # storage location
      GEMINI_API_KEYS=... python3 run.py      # enable AI chat (any adapter key)
      AI_PROVIDER=gemini groq python3 run.py  # optional router preference order
      NO_BROWSER=1 python3 run.py     # don't auto-open the browser
      BIND=127.0.0.1 python3 run.py   # default: loopback only (private)
      BIND=0.0.0.0  python3 run.py    # LAN access (set ASTRA_TOKEN first!)
      ASTRA_TOKEN=... python3 run.py  # protect every /api/* with a token
      ASTRA_SCHEDULER=1 python3 run.py# start the scheduler daemon
      ASTRA_FASTAPI_DOCS=1 python3 run.py  # expose /docs + /openapi.json
      ASTRA_ASGI_THREADS=80 python3 run.py # size the blocking-work pool

This is the only server. FastAPI/uvicorn serves the shared router in
`astra.web`; there is no stdlib http.server fallback. The stack is handed to
the ASGI app, which owns the shutdown path (scheduler stopped, store closed)
via its lifespan; anything this launcher does afterwards is a safety net for
the case where uvicorn never reached startup (e.g. the port was taken).

Requires:  pip install -r requirements.txt
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from astra.bootstrap import build
from astra.web import AGENT_NAME


class _QuietShutdown(logging.Filter):
    """Drop the Ctrl+C shutdown noise from uvicorn.

    When the server is stopped while a browser tab still holds a streaming
    connection open, uvicorn logs "Exception in ASGI application" with a
    CancelledError/KeyboardInterrupt traceback. It is harmless, so hide
    exactly those records and keep every real error visible.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))


def _use_utf8_output() -> None:
    """Make the launcher's own output survive a legacy Windows console.

    Windows consoles default to a non-UTF-8 codec (cp1252), and the
    startup banner below contains emoji, so `print` would raise
    UnicodeEncodeError and abort startup before the server ever binds.
    Reconfigure the streams: a modern console renders the banner, an old
    one degrades to "?" instead of killing the process.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main() -> int:
    _use_utf8_output()
    try:
        import uvicorn
    except ImportError:
        print("FastAPI/uvicorn are required but not installed.")
        print("Install them:  pip install -r requirements.txt")
        return 1

    from astra.web_fastapi import make_app

    stack = build(with_scheduler=os.environ.get("ASTRA_SCHEDULER") == "1")
    store = stack["store"]

    config = stack["config"]
    port = config.getint("PORT", 8787)
    # secure default: loopback only; operator opts into LAN exposure
    bind = config.get("BIND", "127.0.0.1")
    token = config.get("ASTRA_TOKEN") or ""

    app = make_app(stack=stack)
    url = f"http://localhost:{port}/"

    print("=" * 58)
    print(f"  🚀 {AGENT_NAME} running (FastAPI/ASGI)")
    print(f"  👉 Open:  {url}")
    print(f"  🔒 Bind: {bind} | "
          f"API auth: {'token protected' if token else 'OPEN (set ASTRA_TOKEN)'}")
    gw = stack["router"].gateway
    gw_names = [getattr(c, "name", "?") for c in (gw.connections if gw else [])]
    provider_names = [getattr(p, "name", "?") for p in stack["router"].providers]
    print(f"  🧠 Chat pipeline ON (Gateway "
          f"{'ready' if gw is not None and gw.is_usable() else 'not configured — answers unverified'}"
          f" — {len(gw_names)} set"
          f"{': ' + ', '.join(gw_names) if gw_names else ''}) | "
          f"Tools: {len(stack['registry'].list())} | "
          f"AI: {'configured' if stack['router'].providers else 'not configured'}"
          f" — {len(provider_names)} set"
          f"{': ' + ', '.join(provider_names) if provider_names else ''}")
    if stack["scheduler"]:
        sched = stack["scheduler"]
        print(f"  ⏰ Scheduler running ({len(sched.list())} schedule)")
    if os.environ.get("ASTRA_FASTAPI_DOCS") == "1":
        print(f"  📘 Docs:  http://localhost:{port}/docs")
    print("  (Ctrl+C to stop)")
    print("=" * 58)
    sys.stdout.flush()  # show the banner at once even when piped to a log

    if config.get("NO_BROWSER") != "1":
        try:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        except Exception:
            pass

    logging.getLogger("uvicorn.error").addFilter(_QuietShutdown())
    try:
        # close lingering streaming connections quickly on Ctrl+C
        uvicorn.run(app, host=bind, port=port, log_level="warning",
                    timeout_graceful_shutdown=3)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nBye boss! 👋")
    finally:
        # The app owns this via its lifespan; these calls are idempotent
        # (Store.close swallows a second close) and only matter when uvicorn
        # exited before startup ran.
        if stack.get("scheduler"):
            stack["scheduler"].stop()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
