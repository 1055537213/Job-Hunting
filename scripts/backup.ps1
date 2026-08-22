[CmdletBinding()]
param(
    [string]$BackupRoot = "data/backups",
    [switch]$KeepServicesStopped
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$composeArgs = @(
    "compose",
    "--project-name", "job-hunting-agent-production",
    "-f", (Join-Path $repositoryRoot "compose.yaml"),
    "-f", (Join-Path $repositoryRoot "compose.prod.yaml")
)
$backupPath = [IO.Path]::GetFullPath((Join-Path $repositoryRoot $BackupRoot))
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$targetPath = Join-Path $backupPath $timestamp
$postgresDumpPath = Join-Path $targetPath "postgres.dump"
$minioArchivePath = Join-Path $targetPath "minio-data.tar.gz"
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

New-Item -ItemType Directory -Force -Path $targetPath | Out-Null
$servicesStopped = $false
$postgresContainer = $null
$remoteDumpPath = "/tmp/job-agent-$timestamp.dump"

try {
    $postgresContainer = (& docker @composeArgs ps -q postgres | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($postgresContainer)) {
        throw "The production PostgreSQL container is not running. Start the production stack first."
    }

    $postgresUser = if ($env:JOB_AGENT_POSTGRES_USER) { $env:JOB_AGENT_POSTGRES_USER } else { "job_agent" }
    $postgresDb = if ($env:JOB_AGENT_POSTGRES_DB) { $env:JOB_AGENT_POSTGRES_DB } else { "job_agent" }

    # pg_dump runs online and is copied out of the container as a custom-format archive.
    Invoke-Docker -Arguments @(
        "exec", $postgresContainer, "pg_dump", "--format=custom", "--no-owner", "--no-acl",
        "-U", $postgresUser, "-d", $postgresDb, "-f", $remoteDumpPath
    )
    Invoke-Docker -Arguments @("cp", "${postgresContainer}:$remoteDumpPath", $postgresDumpPath)
    Invoke-Docker -Arguments @("exec", $postgresContainer, "rm", "-f", $remoteDumpPath)

    # MinIO's local filesystem archive is taken during a short maintenance window so the
    # object store is not changing while its named volume is copied.
    Invoke-Compose -Arguments @("stop", "reverse-proxy", "web", "worker", "beat", "minio")
    $servicesStopped = $true
    $dockerBackupPath = $targetPath.Replace("\", "/")
    Invoke-Docker -Arguments @(
        "run", "--rm", "-v", "${minioVolume}:/data:ro", "-v", "${dockerBackupPath}:/backup",
        "alpine:3.20", "tar", "czf", "/backup/minio-data.tar.gz", "-C", "/data", "."
    )

    $manifest = [ordered]@{
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        postgres_database = $postgresDb
        postgres_user = $postgresUser
        postgres_dump = [IO.Path]::GetFileName($postgresDumpPath)
        minio_archive = [IO.Path]::GetFileName($minioArchivePath)
        redis_backup = $false
        note = "Redis is a rebuildable broker/cache and is intentionally excluded."
        postgres_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $postgresDumpPath).Hash
        minio_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $minioArchivePath).Hash
    }
    $manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $targetPath "manifest.json") -Encoding UTF8
    Write-Host "Backup created: $targetPath"
}
finally {
    if ($servicesStopped -and -not $KeepServicesStopped) {
        Invoke-Compose -Arguments @("up", "-d", "--no-build")
    }
}
