[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$BackupDirectory,
    [switch]$ConfirmRestore,
    [switch]$KeepServicesStopped,
    [string]$ProjectName = "job-hunting-agent-production",
    [string[]]$ComposeFiles = @()
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

if (-not $ConfirmRestore) {
    throw "Restore is destructive. Re-run with -ConfirmRestore after verifying the backup directory."
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

$backupPath = Resolve-RepositoryPath -Path $BackupDirectory
$postgresDumpPath = Join-Path $backupPath "postgres.dump"
$minioArchivePath = Join-Path $backupPath "minio-data.tar.gz"
$manifestPath = Join-Path $backupPath "manifest.json"

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
foreach ($requiredFile in @($postgresDumpPath, $minioArchivePath, $manifestPath)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Missing backup file: $requiredFile"
    }
}

# Verify all backup evidence before stopping any service or overwriting any data.
try {
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    if ([int]$manifest.schema_version -ne 1) {
        throw "Unsupported manifest schema version: $($manifest.schema_version)"
    }
    if ([string]$manifest.postgres_dump -ne "postgres.dump" -or
        [string]$manifest.minio_archive -ne "minio-data.tar.gz") {
        throw "The manifest contains unsupported backup filenames."
    }
    $postgresHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $postgresDumpPath).Hash
    $minioHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $minioArchivePath).Hash
    if ($postgresHash -ine [string]$manifest.postgres_sha256) {
        throw "PostgreSQL backup SHA-256 mismatch. Restore was not started."
    }
    if ($minioHash -ine [string]$manifest.minio_sha256) {
        throw "MinIO backup SHA-256 mismatch. Restore was not started."
    }
}
catch {
    throw "Backup manifest validation failed: $($_.Exception.Message)"
}
Write-Host "Backup manifest and SHA-256 verification: PASS"

$servicesStopped = $false
$postgresContainer = $null
$remoteDumpCreated = $false
$remoteDumpPath = "/tmp/job-agent-restore.dump"
try {
    $servicesStopped = $true
    Stop-AvailableServices -Services @("reverse-proxy", "web", "worker", "beat")
    Invoke-Compose -Arguments @(
        "up", "-d", "--no-build", "--wait", "--wait-timeout", "120", "postgres", "minio"
    )

    $postgresContainer = Get-ComposeContainerId -Service "postgres"
    $minioContainer = Get-ComposeContainerId -Service "minio"
    $minioVolume = Get-MinioVolumeName -ContainerId $minioContainer
    $postgresUser = if ($env:JOB_AGENT_POSTGRES_USER) { $env:JOB_AGENT_POSTGRES_USER } else { "job_agent" }
    $postgresDb = if ($env:JOB_AGENT_POSTGRES_DB) { $env:JOB_AGENT_POSTGRES_DB } else { "job_agent" }

    Invoke-Docker -Arguments @("cp", $postgresDumpPath, "${postgresContainer}:$remoteDumpPath")
    $remoteDumpCreated = $true
    Invoke-Docker -Arguments @(
        "exec", $postgresContainer, "pg_restore", "--clean", "--if-exists", "--exit-on-error",
        "--no-owner", "--no-acl", "-U", $postgresUser, "-d", $postgresDb, $remoteDumpPath
    )
    Invoke-Docker -Arguments @("exec", $postgresContainer, "rm", "-f", $remoteDumpPath)
    $remoteDumpCreated = $false

    # Restore MinIO only after PostgreSQL succeeds, then apply any migrations newer than the backup.
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
    Invoke-Compose -Arguments @(
        "up", "-d", "--no-build", "--wait", "--wait-timeout", "120", "minio"
    )
    Invoke-Compose -Arguments @("run", "--rm", "migrate")
    Write-Host "Restore completed from: $backupPath"
}
finally {
    if ($remoteDumpCreated -and $postgresContainer) {
        try {
            Invoke-Docker -Arguments @("exec", $postgresContainer, "rm", "-f", $remoteDumpPath)
        }
        catch {
            Write-Warning "Could not remove the temporary PostgreSQL restore file: $($_.Exception.Message)"
        }
    }
    if ($servicesStopped -and -not $KeepServicesStopped) {
        Invoke-Compose -Arguments @("up", "-d", "--no-build")
    }
}
