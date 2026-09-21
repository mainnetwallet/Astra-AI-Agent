# Astra AI Agent — setup for Windows (PowerShell)
#   .\setup.ps1
# Requires: Python 3.9+ installed and on PATH.

Set-Location $PSScriptRoot

Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  Astra AI Agent — Setup (Windows)" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan

# check python
try { python --version | Out-Null } catch {
    Write-Host "`u{26A0}  python not found on PATH. Install from https://python.org and tick 'Add to PATH'." -ForegroundColor Yellow
    exit 1
}

Write-Host "`u{1F4E6} Installing dependencies..."
python -m pip install --upgrade -r requirements.txt
Write-Host "`u{2705} Web server deps installed (fastapi + uvicorn)"

New-Item -ItemType Directory -Force -Path ".\data" | Out-Null
Write-Host "`u{2705} data/ dir ready"

if (-not (Test-Path ".\config.json")) {
    @"
{
  "PORT": 8787,
  "NO_BROWSER": "",
  "BIND": "127.0.0.1"
}
"@ | Set-Content -Encoding UTF8 ".\config.json"
    Write-Host "`u{2705} config.json created (PORT=8787)"
} else {
    Write-Host "`u{2139} config.json already exists — skipping"
}

if (Test-Path ".\.env") {
    Write-Host "`u{1F4A1} .env found — AI providers will be read from it"
} else {
    Write-Host "`u{1F4A1} No .env yet — copy the template and add a provider key:"
    Write-Host "      Copy-Item .env.example .env   # then set e.g. GEMINI_API_KEYS"
}

Write-Host ""
Write-Host "`u{2705} Done! Start with:  .\start.ps1"
Write-Host ""
