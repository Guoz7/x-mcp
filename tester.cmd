@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
if "%~1"=="" (
    echo Usage: tester.cmd setup ^| config ^| doctor ^| login ^| check-login ^| smoke ^| http
    exit /b 0
)
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -X utf8 "%~dp0tester.py" %*
    exit /b !ERRORLEVEL!
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -X utf8 "%~dp0tester.py" %*
    exit /b !ERRORLEVEL!
)
where python >nul 2>nul
if not errorlevel 1 (
    python -X utf8 "%~dp0tester.py" %*
    exit /b !ERRORLEVEL!
)
echo Python 3.11 or newer is required. Install Python, then try again.
exit /b 2
