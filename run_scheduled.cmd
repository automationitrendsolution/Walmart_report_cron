@echo off
REM Windows Task Scheduler entry point. Runs from the project folder so .env is found.
cd /d "%~dp0"
uv run --no-sync python run.py >> logs\scheduler.log 2>&1
exit /b %ERRORLEVEL%
