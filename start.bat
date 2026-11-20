@echo off
rem Start Clipper Studio and open it in the browser. Run install.bat once first.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call install.bat
".venv\Scripts\python.exe" ui\server.py --open %*
pause
