@echo off
REM ============================================================
REM  MangaFactory v2.0 - launcher
REM  Starts the local server and opens the browser UI.
REM  Close this window (or Ctrl+C) to stop the server.
REM ============================================================
title MangaFactory v2.0

REM UTF-8 for this window only, so the app's box-drawing banner renders
REM as lines instead of mojibake.
chcp 65001 >nul

cd /d "%~dp0"

REM Find a python that actually runs. "where python" is not enough on
REM Windows 11: AppData\Local\Microsoft\WindowsApps\python.exe is an App
REM Execution Alias stub that prints a Microsoft Store advert and exits,
REM so every candidate is test-run before it is trusted.
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY (
    python -c "import sys" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
        set "PY="%LOCALAPPDATA%\Programs\Python\Python311\python.exe""
    )
)
if not defined PY (
    echo.
    echo   [X] No working Python 3 was found on this machine.
    echo.
    echo       If typing "python" opens the Microsoft Store, that is the
    echo       alias stub, not Python. Turn it off under Settings ^>
    echo       Apps ^> Advanced app settings ^> App execution aliases.
    echo.
    echo       Otherwise install Python 3.9+ from
    echo       https://www.python.org/downloads/ with "Add python.exe to
    echo       PATH" ticked.
    echo.
    pause
    exit /b 1
)

echo.
echo   MANGAFACTORY v2.0
echo   -----------------
echo   Starting server on http://localhost:5000
echo   The browser will open in a moment.
echo.
echo   Output folder: %USERPROFILE%\Desktop\MangaFactory
echo     Downloaded\  (raw downloads)
echo     exported\    (packaged CBZs)
echo.
echo   Leave this window open while you use it.
echo   Press Ctrl+C to stop.
echo.

REM No browser call here - the app opens its own tab once Flask is up.
%PY% "MangaFactory 2.0.py" %*

echo.
echo   MangaFactory has stopped.
pause
