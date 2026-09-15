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

New-Item -ItemType Directory -Force -Path ".\data" | Out-Null
Write-Host "`u{2705} data/ dir ready"

if (-not (Test-Path ".\config.json")) {
    @"
{
  "PORT": 8787,
  "NO_BROWSER": "",
  "BIND": "0.0.0.0"
}
"@ | Set-Content -Encoding UTF8 ".\config.json"
    Write-Host "`u{2705} config.json created (PORT=8787)"
} else {
    Write-Host "`u{2139} config.json already exists — skipping"
}

if ($env:ANTHROPIC_API_KEY) {
    Write-Host "`u{1F4A1} ANTHROPIC_API_KEY set — AI chat will use Claude"
} else {
    Write-Host "`u{1F4A1} No ANTHROPIC_API_KEY — running offline"
    Write-Host "      Set:  `$env:ANTHROPIC_API_KEY = 'sk-ant-...'"
}

Write-Host ""
Write-Host "`u{2705} Done! Start with:  .\start.ps1"
Write-Host ""