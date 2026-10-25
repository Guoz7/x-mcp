@echo off
setlocal
cd /d "%~dp0"

echo [1/4] Creating the package-local Python environment and MCP config...
call "%~dp0tester.cmd" setup
if errorlevel 1 exit /b %errorlevel%

echo [2/4] Running the credential-free MCP smoke test...
call "%~dp0tester.cmd" smoke
if errorlevel 1 exit /b %errorlevel%

echo [3/4] Checking browser prerequisites...
call "%~dp0tester.cmd" doctor
if errorlevel 1 (
  echo Doctor reported a missing browser prerequisite.
  echo Fix the reported item, then run internal-test.cmd again.
  exit /b %errorlevel%
)

echo [4/4] Finished.
echo Next: run tester.cmd login, sign in to your own X account, close the window,
echo then add the generated mcp-client.json or mcp-codex.toml entry to your agent.
echo Read ONE-CLICK-INTERNAL.md and PROMPTS.md before running a write test.
exit /b 0
