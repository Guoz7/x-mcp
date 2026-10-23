param(
    [int]$Iterations = 500,
    [int]$Clients = 16,
    [int]$Requests = 50
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $root
try {
    python -X utf8 -m py_compile server.py oauth.py browser_bridge.py conformance_fixtures.py mcp_client.py x_api.py x_public.py agent_help_board.py write_store.py composer.py build_insights.py benchmark.py loadtest.py public_e2e.py firefox_only_e2e.py browser_executor.py executor_visual_e2e.py executor_matrix_e2e.py owner_login_check.py owner_login_open.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    python -X utf8 -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    python -X utf8 benchmark.py --iterations $Iterations
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    # Fails with exit code 2 when any HTTP request errors.
    python -X utf8 loadtest.py --clients $Clients --requests $Requests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
