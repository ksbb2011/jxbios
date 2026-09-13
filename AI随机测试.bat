@echo off
chcp 65001 >nul
rem AI random-review test: start the AI decision agent, then the robot.
rem Requires config/runtime.json: ai_review=true AND no_pause_test=false.
rem Agent polls data/review_state.json -> writes data/review_decision.json.
cd /d "%~dp0"
set PY=C:\Users\Lenovo\AppData\Local\Programs\Python\Python311\python.exe
echo [1/2] Starting AI review agent (new window, keep it open)...
start "AI-Review-Agent" "%PY%" tools\ai_review_agent.py
timeout /t 2 >nul
echo [2/2] Starting robot (AI random-test mode). Press Ctrl+C to stop.
"%PY%" main.py run
echo.
echo Done. Press any key to close.
pause >nul
