@echo off
REM Astra AI Agent - quick start for Windows (double-click)
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
    echo python not found - run setup.bat first
    pause
    exit /b 1
)
python run.py
pause