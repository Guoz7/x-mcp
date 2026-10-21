param(
    [switch]$Mcp,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ServerArgs
)

$ErrorActionPreference = "Stop"
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$python = $env:X_STARTER_PYTHON
if (-not $python) {
    $bundled = "C:\Users\54736\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $bundled) {
        $python = $bundled
    } else {
        $python = (Get-Command python -ErrorAction Stop).Source
    }
}

$env:X_STARTER_MODE = "mock"
$server = Join-Path $PSScriptRoot "server.py"

if ($Mcp -or $ServerArgs.Count -gt 0) {
    & $python -I -X utf8 $server @ServerArgs
} else {
    & $python -I -X utf8 $server --demo
}
exit $LASTEXITCODE
