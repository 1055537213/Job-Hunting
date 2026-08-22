[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$BackupDirectory,
    [switch]$ConfirmRestore,
    [switch]$KeepServicesStopped
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $ConfirmRestore) {
    throw "Restore is destructive. Re-run with -ConfirmRestore after verifying the backup directory."
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$composeArgs = @(
    "compose",
    "--project-name", "job-hunting-agent-production",
    "-f", (Join-Path $repositoryRoot "compose.yaml"),
    "-f", (Join-Path $repositoryRoot "compose.prod.yaml")
)
$backupPath = [IO.Path]::GetFullPath($BackupDirectory)
$postgresDumpPath = Join-Path $backupPath "postgres.dump"
$minioArchivePath = Join-Path $backupPath "minio-data.tar.gz"
$minioVolume = "job-hunting-agent-production_minio_prod_data"

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed with exit code ${LASTEXITCODE}: docker $($Arguments -join ' ')"
    }
}

function Invoke-Compose {
    param([Parameter(Mandatory)][string[]]$Arguments)

    Invoke-Docker -Arguments ($composeArgs + $Arguments)
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker command was not found. Start Docker Desktop and try again."
}
if (-not (Test-Path -LiteralPath $postgresDumpPath -PathType Leaf)) {
    throw "Missing PostgreSQL backup: $postgresDumpPath"
}
if (-not (Test-Path -LiteralPath $minioArchivePath -PathType Leaf)) {
    throw "Missing MinIO backup: $minioArchivePath"
}

$servicesStopped = $false
$postgresContainer = $null
$remoteDumpPath = "/tmp/job-agent-restore.dump"
try {
    Invoke-Compose -Arguments @("stop", "reverse-proxy", "web", "worker", "beat")
    $servicesStopped = $true
    Invoke-Compose -Arguments @("up", "-d", "--no-build", "postgres", "minio")

    $postgresContainer = (& docker @composeArgs ps -q postgres | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($postgresContainer)) {
        throw "The production PostgreSQL container could not be started."
    }
    $postgresUser = if ($env:JOB_AGENT_POSTGRES_USER) { $env:JOB_AGENT_POSTGRES_USER } else { "job_agent" }
    $postgresDb = if ($env:JOB_AGENT_POSTGRES_DB) { $env:JOB_AGENT_POSTGRES_DB } else { "job_agent" }

    Invoke-Docker -Arguments @("cp", $postgresDumpPath, "${postgresContainer}:$remoteDumpPath")
    Invoke-Docker -Arguments @(
        "exec", $postgresContainer, "pg_restore", "--clean", "--if-exists", "--no-owner", "--no-acl",
        "-U", $postgresUser, "-d", $postgresDb, $remoteDumpPath
    )
    Invoke-Docker -Arguments @("exec", $postgresContainer, "rm", "-f", $remoteDumpPath)

    # Restore the object volume only after the database restore has completed.
    Invoke-Compose -Arguments @("stop", "minio")
    Invoke-Docker -Arguments @(
        "run", "--rm", "-v", "${minioVolume}:/data", "alpine:3.20", "sh", "-c",
        "find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +"
    )
    $dockerBackupPath = $backupPath.Replace("\", "/")
    Invoke-Docker -Arguments @(
        "run", "--rm", "-v", "${minioVolume}:/data", "-v", "${dockerBackupPath}:/backup:ro",
        "alpine:3.20", "tar", "xzf", "/backup/minio-data.tar.gz", "-C", "/data"
    )
    Invoke-Compose -Arguments @("up", "-d", "--no-build", "minio")
    Invoke-Compose -Arguments @("run", "--rm", "migrate")
    Write-Host "Restore completed from: $backupPath"
}
finally {
    if ($servicesStopped -and -not $KeepServicesStopped) {
        Invoke-Compose -Arguments @("up", "-d", "--no-build")
    }
}
