$ErrorActionPreference = "Stop"
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = [Text.Encoding]::UTF8

& (Join-Path $PSScriptRoot "run_mock.ps1")
exit $LASTEXITCODE
