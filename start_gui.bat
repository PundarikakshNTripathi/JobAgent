@echo off
rem Opens JobAgent in its own app window (starts the local server in the background if needed).
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" agent\run.py app
) else (
  python agent\run.py app
)
