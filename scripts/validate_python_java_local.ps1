[CmdletBinding()]
param(
    [string]$Python = $(if (Test-Path -LiteralPath "E:\Anaconda\python.exe") { "E:\Anaconda\python.exe" } else { "python" }),
    [int]$Port = 18081
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

Write-Host "==> Running isolated local Python + Java acceptance"
& $Python (Join-Path $PSScriptRoot "validate_python_java_local.py") --port $Port
if ($LASTEXITCODE -ne 0) {
    throw "Local Python + Java acceptance failed with exit code $LASTEXITCODE."
}
Write-Host "Local Python + Java acceptance: PASS"
