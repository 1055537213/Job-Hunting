[CmdletBinding()]
param(
    [string]$BackupRoot = "data/backups",
    [string]$ProjectName = "job-hunting-agent-production",
    [string[]]$ComposeFiles = @(),
    [switch]$KeepServicesStopped
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot

function Resolve-RepositoryPath {
    param([Parameter(Mandatory)][string]$Path)

    $candidate = if ([IO.Path]::IsPathRooted($Path)) {
        $Path
    }
    else {
        Join-Path $repositoryRoot $Path
    }
    return [IO.Path]::GetFullPath($candidate)
}

if ($ProjectName -notmatch '^[a-z0-9][a-z0-9_-]*$') {
    throw "Invalid Compose project name: $ProjectName"
}
if ($ComposeFiles.Count -eq 0) {
    $ComposeFiles = @("compose.yaml", "compose.prod.yaml")
}

$composeArgs = @("compose", "--project-name", $ProjectName)
foreach ($composeFile in $ComposeFiles) {
    $resolvedComposeFile = Resolve-RepositoryPath -Path $composeFile
    if (-not (Test-Path -LiteralPath $resolvedComposeFile -PathType Leaf)) {
        throw "Compose file was not found: $resolvedComposeFile"
    }
    $composeArgs += @("-f", $resolvedComposeFile)
}

$backupPath = Resolve-RepositoryPath -Path $BackupRoot
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$targetPath = Join-Path $backupPath $timestamp
$postgresDumpPath = Join-Path $targetPath "postgres.dump"
$minioArchivePath = Join-Path $targetPath "minio-data.tar.gz"

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed with exit code ${LASTEXITCODE}: docker $($Arguments -join ' ')"
    }
}

function Invoke-DockerOutput {
    param([Parameter(Mandatory)][string[]]$Arguments)

    $output = @(& docker @Arguments)
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed with exit code ${LASTEXITCODE}: docker $($Arguments -join ' ')"
    }
    return $output
}

function Invoke-Compose {
    param([Parameter(Mandatory)][string[]]$Arguments)

    Invoke-Docker -Arguments ($composeArgs + $Arguments)
}

function Get-ComposeContainerId {
    param([Parameter(Mandatory)][string]$Service)

    $containerId = ((Invoke-DockerOutput -Arguments ($composeArgs + @("ps", "-q", $Service))) -join "`n").Trim()
    if ([string]::IsNullOrWhiteSpace($containerId)) {
        throw "The $Service container for Compose project '$ProjectName' is not running."
    }
    return $containerId
}

function Get-MinioVolumeName {
    param([Parameter(Mandatory)][string]$ContainerId)

    $inspection = ((Invoke-DockerOutput -Arguments @("inspect", $ContainerId)) -join "`n") | ConvertFrom-Json
    $container = @($inspection)[0]
    $mounts = @($container.Mounts | Where-Object { $_.Destination -eq "/data" -and $_.Type -eq "volume" })
    if ($mounts.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$mounts[0].Name)) {
        throw "The MinIO /data mount is not a Docker named volume."
    }
    return [string]$mounts[0].Name
}

function Stop-AvailableServices {
    param([Parameter(Mandatory)][string[]]$Services)

    $available = @(Invoke-DockerOutput -Arguments ($composeArgs + @("config", "--services")))
    $toStop = @($Services | Where-Object { $available -contains $_ })
    if ($toStop.Count -gt 0) {
        Invoke-Compose -Arguments (@("stop") + $toStop)
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker command was not found. Start Docker Desktop and try again."
}

New-Item -ItemType Directory -Force -Path $targetPath | Out-Null
$servicesStopped = $false
$remoteDumpCreated = $false
$postgresContainer = $null
$remoteDumpPath = "/tmp/job-agent-$timestamp.dump"

try {
    $postgresContainer = Get-ComposeContainerId -Service "postgres"
    $minioContainer = Get-ComposeContainerId -Service "minio"
    $minioVolume = Get-MinioVolumeName -ContainerId $minioContainer

    $postgresUser = if ($env:JOB_AGENT_POSTGRES_USER) { $env:JOB_AGENT_POSTGRES_USER } else { "job_agent" }
    $postgresDb = if ($env:JOB_AGENT_POSTGRES_DB) { $env:JOB_AGENT_POSTGRES_DB } else { "job_agent" }

    # PostgreSQL remains online while pg_dump creates a transactionally consistent snapshot.
    Invoke-Docker -Arguments @(
        "exec", $postgresContainer, "pg_dump", "--format=custom", "--no-owner", "--no-acl",
        "-U", $postgresUser, "-d", $postgresDb, "-f", $remoteDumpPath
    )
    $remoteDumpCreated = $true
    Invoke-Docker -Arguments @("cp", "${postgresContainer}:$remoteDumpPath", $postgresDumpPath)
    Invoke-Docker -Arguments @("exec", $postgresContainer, "rm", "-f", $remoteDumpPath)
    $remoteDumpCreated = $false

    # MinIO is stopped briefly so the filesystem archive cannot contain partially written objects.
    $servicesStopped = $true
    Stop-AvailableServices -Services @("reverse-proxy", "web", "worker", "beat", "minio")
    $dockerBackupPath = $targetPath.Replace("\", "/")
    Invoke-Docker -Arguments @(
        "run", "--rm", "-v", "${minioVolume}:/data:ro", "-v", "${dockerBackupPath}:/backup",
        "alpine:3.20", "tar", "czf", "/backup/minio-data.tar.gz", "-C", "/data", "."
    )

    $manifest = [ordered]@{
        schema_version = 1
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        compose_project = $ProjectName
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
    if ($remoteDumpCreated -and $postgresContainer) {
        try {
            Invoke-Docker -Arguments @("exec", $postgresContainer, "rm", "-f", $remoteDumpPath)
        }
        catch {
            Write-Warning "Could not remove the temporary PostgreSQL dump: $($_.Exception.Message)"
        }
    }
    if ($servicesStopped -and -not $KeepServicesStopped) {
        Invoke-Compose -Arguments @("up", "-d", "--no-build")
    }
}
