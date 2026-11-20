@echo off
setlocal
cd /d "%~dp0"
set X_STARTER_MODE=browser
set X_STARTER_ENABLE_BROWSER=1
echo Starting X MCP lightweight dashboard on http://127.0.0.1:8765/
start "" http://127.0.0.1:8765/
py -3.12 -I -X utf8 server.py --transport http --port 8765
