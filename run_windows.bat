@echo off
rem Prompt Like A PRO - starts BOTH the backend LLM gateway and the game app.
rem First run creates the gateway virtualenv automatically (needs internet once).
cd /d "%~dp0"
set PLAP_OPEN=1

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 run_stack.py
) else (
  python run_stack.py
)
pause
