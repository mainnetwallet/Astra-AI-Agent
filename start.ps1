# Astra AI Agent - one-command start for Windows (PowerShell)
#   .\start.ps1              -> port 8787
#   $env:PORT="9000"; .\start.ps1
#   $env:ASTRA_SCHEDULER="1"; .\start.ps1   # scheduler daemon on
# Keep this file ASCII-only (Windows PowerShell 5.1 compatibility).
Set-Location $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "python not found - run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}

python run.py
