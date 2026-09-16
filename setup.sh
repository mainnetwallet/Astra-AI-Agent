#!/usr/bin/env bash
# Astra AI Agent — setup script (Linux / Termux / macOS)
# Installs Python 3.9+ if missing, creates data dir, optional deps.
set -e
cd "$(dirname "$0")"

echo "══════════════════════════════════════════════════════"
echo "  🚀 Astra AI Agent — Setup"
echo "══════════════════════════════════════════════════════"

# check python3
if ! command -v python3 >/dev/null 2>&1; then
    echo "⚠️  python3 not found. Trying to install…"
    if command -v pkg >/dev/null 2>&1; then
        # Termux
        pkg update -y && pkg install -y python
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -qq && sudo apt-get install -y python3 python3-venv
    elif command -v brew >/dev/null 2>&1; then
        brew install python
    else
        echo "❌ python3 not found and could not auto-install."
        echo "   Install Python 3.9+ manually and re-run."
        exit 1
    fi
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "✅ Python $PYTHON_VERSION found"

# data dir
DATA_DIR="${DATA_DIR:-./data}"
mkdir -p "$DATA_DIR"
echo "✅ Data dir: $DATA_DIR"

# create config.json if missing
if [ ! -f config.json ]; then
    cat > config.json <<'CONF'
{
  "PORT": 8787,
  "NO_BROWSER": "",
  "BIND": "127.0.0.1"
}
CONF
    echo "✅ config.json created (PORT=8787)"
else
    echo "ℹ️  config.json already exists — skipping"
fi

# optional: install Anthropic SDK for AI Q&A (offline works without it)
if [ -n "$ANTHROPIC_API_KEY" ]; then
    echo "💡 ANTHROPIC_API_KEY detected — AI chat will use Claude"
else
    echo "💡 No ANTHROPIC_API_KEY — running offline (no AI chat)"
    echo "   Set it later: export ANTHROPIC_API_KEY=sk-ant-..."
fi

echo ""
echo "✅ Setup complete! Start with:  ./start.sh"
echo ""
