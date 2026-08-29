param(
    [ValidateSet("smoke", "full")]
    [string]$Profile = "smoke",
    [string]$Python = "python",
    [string]$Concurrency = "",
    [ValidateRange(1, 20)]
    [int]$RequestsPerUser = 1,
    [ValidateRange(1, 16)]
    [int]$WorkerConcurrency = 2,
    [ValidateRange(5, 300)]
    [int]$TaskTimeoutSeconds = 45,
    [string]$DatabaseUrl = "",
    [string]$RedisUrl = "",
    [string]$WorkerDatabaseUrl = "",
    [string]$WorkerRedisUrl = "",
    [string]$OutputDirectory = "",
    [switch]$SkipWorker,
    [switch]$SkipFaults
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$Root = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $Root "data\eval-reports"
}

if (-not $SkipWorker) {
    Write-Host "==> Ensuring PostgreSQL and Redis are available"
    & docker compose -f (Join-Path $Root "compose.yaml") -f (Join-Path $Root "compose.dev.yaml") up -d postgres redis
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to start PostgreSQL and Redis for the end-to-end load test."
    }
}

$Arguments = @(
    "-m", "job_hunting_agent.e2e_load_runner",
    "--profile", $Profile,
    "--requests-per-user", $RequestsPerUser,
    "--worker-concurrency", $WorkerConcurrency,
    "--task-timeout-seconds", $TaskTimeoutSeconds,
    "--report-dir", $OutputDirectory
)
if ($Concurrency) {
    $Arguments += @("--concurrency", $Concurrency)
}
if ($DatabaseUrl) {
    $Arguments += @("--database-url", $DatabaseUrl)
}
if ($RedisUrl) {
    $Arguments += @("--redis-url", $RedisUrl)
}
if ($WorkerDatabaseUrl) {
    $Arguments += @("--worker-database-url", $WorkerDatabaseUrl)
}
if ($WorkerRedisUrl) {
    $Arguments += @("--worker-redis-url", $WorkerRedisUrl)
}
if ($SkipWorker) {
    $Arguments += "--skip-worker"
}
if ($SkipFaults) {
    $Arguments += "--skip-faults"
}

$PreviousPythonPath = $env:PYTHONPATH
try {
    $SourcePath = Join-Path $Root "src"
    $env:PYTHONPATH = if ($PreviousPythonPath) {
        "$SourcePath$([IO.Path]::PathSeparator)$PreviousPythonPath"
    }
    else {
        $SourcePath
    }
    Write-Host "==> Running isolated $Profile end-to-end load test"
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "End-to-end load acceptance failed with exit code $LASTEXITCODE."
    }
}
finally {
    $env:PYTHONPATH = $PreviousPythonPath
}
