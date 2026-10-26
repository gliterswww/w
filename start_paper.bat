@echo off
rem NQ Autotrader: double-click to start the dashboard and the paper-trading engine.
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo First run: setting up Python environment...
    python -m venv .venv || goto :nopython
    .venv\Scripts\python -m pip install -r requirements.txt || goto :failed
)

start "NQ Autotrader - dashboard (keep open)" cmd /k .venv\Scripts\python -m nqtrader serve
start "NQ Autotrader - paper engine (keep open)" cmd /k .venv\Scripts\python -m nqtrader paper
.venv\Scripts\python -m nqtrader refresh >nul 2>&1
timeout /t 4 >nul
start "" http://127.0.0.1:8000
exit /b 0

:nopython
echo Python was not found. Install it from python.org and tick "Add python.exe to PATH".
pause
exit /b 1

:failed
echo Installing requirements failed. Copy the error above and send it to Claude.
pause
exit /b 1
