@echo off
rem One-click setup on Windows. Needs Python 3.9+ from python.org ("Add python.exe to PATH" ticked).
cd /d "%~dp0"
where py >nul 2>nul && (py -3 install.py %* & goto end)
where python >nul 2>nul && (python install.py %* & goto end)
echo Python 3.9 or newer was not found. Install it from https://www.python.org/downloads/ and run install.bat again.
:end
pause
