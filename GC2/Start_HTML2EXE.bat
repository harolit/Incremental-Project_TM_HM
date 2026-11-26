@echo off
title HTML2EXE
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set PY=py
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python was not found. Install Python 3.11 or 3.12 from https://www.python.org/downloads/
        echo IMPORTANT: tick "Add python.exe to PATH" during installation.
        pause
        exit /b 1
    )
    set PY=python
)

echo Checking dependencies (first run may take a minute)...
%PY% -m pip install --quiet --upgrade pywebview pyinstaller pillow
if errorlevel 1 (
    echo Failed to install dependencies. Check your internet connection.
    pause
    exit /b 1
)

start "" %PY% html2exe.py
exit /b 0
