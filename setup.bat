@echo off
REM Astra AI Agent - setup for Windows (double-click)
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] python not found on PATH.
    echo Install from https://python.org and tick "Add python.exe to PATH",
    echo then close and re-open this window and run setup.bat again.
    echo.
    pause
    exit /b 1
)
if not exist data mkdir data
echo.
echo [..] Installing dependencies...
python -m pip install --upgrade -r requirements.txt
if errorlevel 1 echo [WARN] pip install failed - run it manually: python -m pip install -r requirements.txt
if not exist config.json (
    echo {"PORT": 8787, "NO_BROWSER": "", "BIND": "127.0.0.1"}> config.json
    echo [OK] config.json created
) else (
    echo [i] config.json already exists
)
echo.
echo [OK] Setup complete! Double-click start.bat
pause
