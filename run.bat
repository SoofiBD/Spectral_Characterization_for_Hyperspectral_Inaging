@echo off
REM Spectral Characterization Suite launcher for Windows (double-click or: run.bat --simulate)
cd /d "%~dp0"

if not exist .venv (
    echo Creating virtual environment ...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Python 3.11+ is required: https://www.python.org/downloads/
        pause
        exit /b 1
    )
)

if not exist .venv\.deps_installed (
    echo Installing dependencies ...
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt || (pause & exit /b 1)
    echo.> .venv\.deps_installed
)

.venv\Scripts\python.exe main.py %*
if errorlevel 1 pause
