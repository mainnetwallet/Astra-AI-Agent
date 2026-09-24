# Astra AI Agent - setup for Windows (PowerShell 5.1 and 7+)
#   .\setup.ps1
# Requires: Python 3.9+ installed and on PATH.
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 reads BOM-less
# files as ANSI, and `u{..} escapes only exist in PowerShell 7).

Set-Location $PSScriptRoot

Write-Host "=============================================="  -ForegroundColor Cyan
Write-Host "  Astra AI Agent - Setup (Windows)"              -ForegroundColor Cyan
Write-Host "=============================================="  -ForegroundColor Cyan

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "[!] python not found on PATH. Install from https://python.org and tick 'Add python.exe to PATH'." -ForegroundColor Yellow
    exit 1
}

Write-Host "[..] Installing dependencies..."
python -m pip install --upgrade -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] pip install failed - run manually: python -m pip install -r requirements.txt" -ForegroundColor Yellow
} else {
    Write-Host "[OK] Dependencies installed"
}

New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "data") | Out-Null
Write-Host "[OK] data/ dir ready"

$cfg = Join-Path $PSScriptRoot "config.json"
if (-not (Test-Path $cfg)) {
    $json = @'
{
  "PORT": 8787,
  "NO_BROWSER": "",
  "BIND": "127.0.0.1"
}
'@
    [System.IO.File]::WriteAllText($cfg, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "[OK] config.json created (PORT=8787)"
} else {
    Write-Host "[i] config.json already exists - skipping"
}

if (Test-Path (Join-Path $PSScriptRoot ".env")) {
    Write-Host "[i] .env found - AI providers will be read from it"
} else {
    Write-Host "[i] No .env yet - copy the template and add a provider key:"
    Write-Host "      Copy-Item .env.example .env   # then set e.g. GEMINI_API_KEYS"
}

Write-Host ""
Write-Host "[OK] Done! Start with:  .\start.ps1"
Write-Host ""
