param(
    [int]$Iterations = 500,
    [int]$Clients = 16,
    [int]$Requests = 50
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = if ($env:X_STARTER_PYTHON) { $env:X_STARTER_PYTHON } else { "python" }
Push-Location $root
try {
    & $python -X utf8 -m compileall -q .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & $python -I -X utf8 scripts/release_check.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & $python -X utf8 -m unittest discover -s tests -p "test_*.py" -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & $python -X utf8 scripts/benchmark.py --iterations $Iterations
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    # Fails with exit code 2 when any HTTP request errors.
    & $python -X utf8 scripts/loadtest.py --clients $Clients --requests $Requests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
