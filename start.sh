#!/usr/bin/env bash
# Astra AI Agent — one-command start (Linux / Termux / macOS)
#
#   ./start.sh                     -> port 8787, browser auto-opens
#   PORT=9000 ./start.sh           -> custom port
#   ASTRA_SCHEDULER=1 ./start.sh   -> enable the scheduler daemon (workflows,
#                                     deadline reminders, recurring checks)
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ python3 not found — run ./setup.sh first"
    exit 1
fi

# allow scheduler via env (scheduler is off by default)
if [ "$ASTRA_SCHEDULER" = "1" ]; then
    exec python3 run.py
else
    ASTRA_SCHEDULER=0 exec python3 run.py
fi