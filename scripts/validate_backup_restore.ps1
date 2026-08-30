[CmdletBinding()]
param(
    [ValidateRange(60, 900)]
    [int]$TimeoutSeconds = 240,
    [string]$EvidenceRoot = "data/recovery-drills"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$suffix = [guid]::NewGuid().ToString("N").Substring(0, 12)
$drillId = "backup-restore-$suffix"
$projectName = "job-hunting-agent-recovery-$suffix"
$composeFiles = @(
    (Join-Path $repositoryRoot "compose.yaml"),
    (Join-Path $repositoryRoot "compose.dev.yaml"),
    (Join-Path $repositoryRoot "compose.recovery-test.yaml")
)
$composeArgs = @("compose", "--project-name", $projectName)
foreach ($composeFile in $composeFiles) {
    $composeArgs += @("-f", $composeFile)
}

$evidenceBase = if ([IO.Path]::IsPathRooted($EvidenceRoot)) {
    [IO.Path]::GetFullPath($EvidenceRoot)
}
else {
    [IO.Path]::GetFullPath((Join-Path $repositoryRoot $EvidenceRoot))
}
$runPath = Join-Path $evidenceBase ((Get-Date -Format "yyyyMMdd-HHmmss") + "-$suffix")
$backupRoot = Join-Path $runPath "backup"
$reportPath = Join-Path $runPath "recovery-report.json"
$backupScript = Join-Path $PSScriptRoot "backup.ps1"
$restoreScript = Join-Path $PSScriptRoot "restore.ps1"
$startedAt = (Get-Date).ToUniversalTime()
$backupSeconds = $null
$recoverySeconds = $null
$backupDirectory = $null
$failure = $null
$cleanupSucceeded = $false
$checks = [ordered]@{
    missing_manifest_rejected = $false
    postgres_dump_tamper_rejected = $false
    manifest_tamper_rejected = $false
    services_untouched_after_preflight_failure = $false
    postgres_snapshot_restored = $false
    post_backup_database_change_removed = $false
    minio_object_restored = $false
    post_backup_object_removed = $false
    current_migration_applied = $false
    queued_task_state_restored = $false
}

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Invoke-DockerOutput {
    param([Parameter(Mandatory)][string[]]$Arguments)

    $output = @(& docker @Arguments)
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
    return $output
}

function Invoke-Compose {
    param([Parameter(Mandatory)][string[]]$Arguments)

    Invoke-Docker -Arguments ($composeArgs + $Arguments)
}

function Invoke-ComposeOutput {
    param([Parameter(Mandatory)][string[]]$Arguments)

    return @(Invoke-DockerOutput -Arguments ($composeArgs + $Arguments))
}

function Wait-ForInfrastructure {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $postgresId = ((Invoke-ComposeOutput -Arguments @("ps", "--status", "running", "-q", "postgres")) -join "`n").Trim()
        $minioId = ((Invoke-ComposeOutput -Arguments @("ps", "--status", "running", "-q", "minio")) -join "`n").Trim()
        if ($postgresId -and $minioId) {
            $postgresHealth = (& docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" $postgresId).Trim()
            $minioHealth = (& docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" $minioId).Trim()
            if ($LASTEXITCODE -eq 0 -and $postgresHealth -eq "healthy" -and $minioHealth -eq "healthy") {
                return
            }
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for isolated PostgreSQL and MinIO health checks."
}

function Invoke-ProbePython {
    param(
        [Parameter(Mandatory)][string]$Code,
        [hashtable]$Environment = @{}
    )

    $codeB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
    $arguments = @("run", "--rm", "--no-deps", "-e", "JOB_AGENT_RECOVERY_CODE_B64=$codeB64")
    foreach ($entry in $Environment.GetEnumerator()) {
        $arguments += @("-e", "$($entry.Key)=$($entry.Value)")
    }
    $arguments += @(
        "worker", "python", "-c",
        "import base64,os;exec(base64.b64decode(os.environ['JOB_AGENT_RECOVERY_CODE_B64']))"
    )
    return @(Invoke-ComposeOutput -Arguments $arguments)
}

$seedCode = @'
import json
import os

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import create_engine, text

engine = create_engine(os.environ["JOB_AGENT_DATABASE_URL"])
with engine.begin() as conn:
    account_id = conn.execute(
        text(
            "INSERT INTO accounts (email, password_hash, display_name) "
            "VALUES (:email, :password_hash, :display_name) RETURNING id"
        ),
        {
            "email": os.environ["RECOVERY_SEED_EMAIL"],
            "password_hash": "recovery-drill-not-a-real-password-hash",
            "display_name": "Backup restore drill",
        },
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO background_tasks ("
            "task_key, account_id, task_type, status, progress, attempt, max_attempts, "
            "idempotency_key, payload_json, result_json, created_at, updated_at"
            ") VALUES ("
            ":task_key, :account_id, 'rag_index', 'queued', 0, 0, 3, "
            ":idempotency_key, CAST(:payload AS JSONB), CAST(:result AS JSONB), "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP"
            ")"
        ),
        {
            "task_key": os.environ["RECOVERY_TASK_KEY"],
            "account_id": account_id,
            "idempotency_key": os.environ["RECOVERY_TASK_KEY"],
            "payload": '{"long_text_ids":[1]}',
            "result": '{}',
        },
    )

client = boto3.client(
    "s3",
    endpoint_url=os.environ["JOB_AGENT_OBJECT_STORAGE_ENDPOINT"],
    aws_access_key_id=os.environ["JOB_AGENT_OBJECT_STORAGE_ACCESS_KEY"],
    aws_secret_access_key=os.environ["JOB_AGENT_OBJECT_STORAGE_SECRET_KEY"],
    region_name=os.environ.get("JOB_AGENT_OBJECT_STORAGE_REGION", "us-east-1"),
)
bucket = os.environ["JOB_AGENT_OBJECT_STORAGE_BUCKET"]
try:
    client.head_bucket(Bucket=bucket)
except ClientError:
    client.create_bucket(Bucket=bucket)
client.put_object(
    Bucket=bucket,
    Key=os.environ["RECOVERY_SEED_KEY"],
    Body=os.environ["RECOVERY_SEED_CONTENT"].encode("utf-8"),
    ContentType="text/plain",
)
print(json.dumps({
    "account_id": account_id,
    "bucket": bucket,
    "task_key": os.environ["RECOVERY_TASK_KEY"],
}, ensure_ascii=False))
'@

$mutateCode = @'
import os

import boto3
from sqlalchemy import create_engine, text

engine = create_engine(os.environ["JOB_AGENT_DATABASE_URL"])
with engine.begin() as conn:
    conn.execute(text("DELETE FROM accounts WHERE email = :email"), {"email": os.environ["RECOVERY_SEED_EMAIL"]})
    conn.execute(
        text(
            "INSERT INTO accounts (email, password_hash, display_name) "
            "VALUES (:email, :password_hash, :display_name)"
        ),
        {
            "email": os.environ["RECOVERY_POST_EMAIL"],
            "password_hash": "post-backup-not-a-real-password-hash",
            "display_name": "Post-backup record",
        },
    )
    conn.execute(
        text(
            "UPDATE background_tasks SET status = 'succeeded', progress = 100, "
            "result_json = CAST(:result AS JSONB), finished_at = CURRENT_TIMESTAMP, "
            "updated_at = CURRENT_TIMESTAMP WHERE task_key = :task_key"
        ),
        {
            "task_key": os.environ["RECOVERY_TASK_KEY"],
            "result": '{"mutated_after_backup":true}',
        },
    )

client = boto3.client(
    "s3",
    endpoint_url=os.environ["JOB_AGENT_OBJECT_STORAGE_ENDPOINT"],
    aws_access_key_id=os.environ["JOB_AGENT_OBJECT_STORAGE_ACCESS_KEY"],
    aws_secret_access_key=os.environ["JOB_AGENT_OBJECT_STORAGE_SECRET_KEY"],
    region_name=os.environ.get("JOB_AGENT_OBJECT_STORAGE_REGION", "us-east-1"),
)
bucket = os.environ["JOB_AGENT_OBJECT_STORAGE_BUCKET"]
client.put_object(Bucket=bucket, Key=os.environ["RECOVERY_SEED_KEY"], Body=b"corrupted-after-backup")
client.put_object(Bucket=bucket, Key=os.environ["RECOVERY_POST_KEY"], Body=b"created-after-backup")
'@

$verifyCode = @'
import json
import os

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import create_engine, text

engine = create_engine(os.environ["JOB_AGENT_DATABASE_URL"])
with engine.connect() as conn:
    seed_count = conn.execute(
        text("SELECT COUNT(*) FROM accounts WHERE email = :email"),
        {"email": os.environ["RECOVERY_SEED_EMAIL"]},
    ).scalar_one()
    post_count = conn.execute(
        text("SELECT COUNT(*) FROM accounts WHERE email = :email"),
        {"email": os.environ["RECOVERY_POST_EMAIL"]},
    ).scalar_one()
    revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    task = conn.execute(
        text(
            "SELECT status, progress, result_json FROM background_tasks "
            "WHERE task_key = :task_key"
        ),
        {"task_key": os.environ["RECOVERY_TASK_KEY"]},
    ).mappings().one()

client = boto3.client(
    "s3",
    endpoint_url=os.environ["JOB_AGENT_OBJECT_STORAGE_ENDPOINT"],
    aws_access_key_id=os.environ["JOB_AGENT_OBJECT_STORAGE_ACCESS_KEY"],
    aws_secret_access_key=os.environ["JOB_AGENT_OBJECT_STORAGE_SECRET_KEY"],
    region_name=os.environ.get("JOB_AGENT_OBJECT_STORAGE_REGION", "us-east-1"),
)
bucket = os.environ["JOB_AGENT_OBJECT_STORAGE_BUCKET"]
seed_content = client.get_object(Bucket=bucket, Key=os.environ["RECOVERY_SEED_KEY"])["Body"].read().decode("utf-8")
post_object_absent = False
try:
    client.head_object(Bucket=bucket, Key=os.environ["RECOVERY_POST_KEY"])
except ClientError as exc:
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = exc.response.get("Error", {}).get("Code")
    post_object_absent = status == 404 or code in {"404", "NoSuchKey", "NotFound"}

print(json.dumps({
    "seed_count": int(seed_count),
    "post_count": int(post_count),
    "revision": revision,
    "seed_content": seed_content,
    "post_object_absent": post_object_absent,
    "task_status": task["status"],
    "task_progress": int(task["progress"]),
    "task_result": task["result_json"],
}, ensure_ascii=False))
'@

$seedEmail = "recovery-seed-$suffix@example.invalid"
$postEmail = "recovery-post-$suffix@example.invalid"
$seedKey = "recovery-drill/$suffix/snapshot.txt"
$postKey = "recovery-drill/$suffix/post-backup.txt"
$seedContent = "job-agent-recovery-snapshot-$suffix"
$taskKey = "recovery-task-$suffix"
$probeEnvironment = @{
    RECOVERY_SEED_EMAIL = $seedEmail
    RECOVERY_POST_EMAIL = $postEmail
    RECOVERY_SEED_KEY = $seedKey
    RECOVERY_POST_KEY = $postKey
    RECOVERY_SEED_CONTENT = $seedContent
    RECOVERY_TASK_KEY = $taskKey
}

New-Item -ItemType Directory -Force -Path $runPath | Out-Null

try {
    Write-Host "==> Validating the isolated Compose topology"
    Invoke-Docker -Arguments @("version")
    Invoke-Compose -Arguments @("config", "--quiet")

    Write-Host "==> Starting isolated PostgreSQL and MinIO"
    Invoke-Compose -Arguments @("up", "-d", "postgres", "minio")
    Wait-ForInfrastructure
    Invoke-Compose -Arguments @("run", "--rm", "migrate")

    Write-Host "==> Writing one database record and one real MinIO object"
    $seedOutput = Invoke-ProbePython -Code $seedCode -Environment $probeEnvironment
    $seed = (($seedOutput | Select-Object -Last 1) | ConvertFrom-Json)
    if (-not $seed.account_id) {
        throw "The seed probe did not create an account."
    }

    Write-Host "==> Creating PostgreSQL and MinIO backup evidence"
    $backupStarted = Get-Date
    & $backupScript -BackupRoot $backupRoot -ProjectName $projectName -ComposeFiles $composeFiles -KeepServicesStopped
    $backupSeconds = [Math]::Round(((Get-Date) - $backupStarted).TotalSeconds, 3)
    $backupDirectories = @(Get-ChildItem -LiteralPath $backupRoot -Directory)
    if ($backupDirectories.Count -ne 1) {
        throw "Expected exactly one backup directory, found $($backupDirectories.Count)."
    }
    $backupDirectory = $backupDirectories[0].FullName

    Write-Host "==> Proving missing and tampered backup evidence is rejected before services are touched"
    Invoke-Compose -Arguments @("up", "-d", "postgres", "minio")
    Wait-ForInfrastructure
    $manifestPath = Join-Path $backupDirectory "manifest.json"
    $validManifestPath = "$manifestPath.valid"
    Move-Item -LiteralPath $manifestPath -Destination $validManifestPath
    try {
        & $restoreScript -BackupDirectory $backupDirectory -ProjectName $projectName -ComposeFiles $composeFiles -ConfirmRestore -KeepServicesStopped
        throw "The restore script accepted a backup without manifest.json."
    }
    catch {
        if ($_.Exception.Message -notmatch "Missing backup file") {
            throw
        }
        $checks.missing_manifest_rejected = $true
    }
    finally {
        Move-Item -LiteralPath $validManifestPath -Destination $manifestPath
    }
    Wait-ForInfrastructure

    $dumpPath = Join-Path $backupDirectory "postgres.dump"
    $validDumpPath = "$dumpPath.valid"
    Move-Item -LiteralPath $dumpPath -Destination $validDumpPath
    Copy-Item -LiteralPath $validDumpPath -Destination $dumpPath
    Add-Content -LiteralPath $dumpPath -Value "tamper" -NoNewline
    try {
        & $restoreScript -BackupDirectory $backupDirectory -ProjectName $projectName -ComposeFiles $composeFiles -ConfirmRestore -KeepServicesStopped
        throw "The restore script accepted a tampered PostgreSQL dump."
    }
    catch {
        if ($_.Exception.Message -notmatch "SHA-256 mismatch") {
            throw
        }
        $checks.postgres_dump_tamper_rejected = $true
    }
    finally {
        Remove-Item -LiteralPath $dumpPath -Force
        Move-Item -LiteralPath $validDumpPath -Destination $dumpPath
    }
    Wait-ForInfrastructure

    $archivePath = Join-Path $backupDirectory "minio-data.tar.gz"
    $validArchivePath = "$archivePath.valid"
    Move-Item -LiteralPath $archivePath -Destination $validArchivePath
    Copy-Item -LiteralPath $validArchivePath -Destination $archivePath
    Add-Content -LiteralPath $archivePath -Value "tamper" -NoNewline
    try {
        & $restoreScript -BackupDirectory $backupDirectory -ProjectName $projectName -ComposeFiles $composeFiles -ConfirmRestore -KeepServicesStopped
        throw "The restore script accepted a tampered MinIO archive."
    }
    catch {
        if ($_.Exception.Message -notmatch "SHA-256 mismatch") {
            throw
        }
        $checks.manifest_tamper_rejected = $true
    }
    finally {
        Remove-Item -LiteralPath $archivePath -Force
        Move-Item -LiteralPath $validArchivePath -Destination $archivePath
    }
    Wait-ForInfrastructure
    $checks.services_untouched_after_preflight_failure = $true

    Write-Host "==> Mutating both stores after the backup point"
    $null = Invoke-ProbePython -Code $mutateCode -Environment $probeEnvironment

    Write-Host "==> Restoring the isolated snapshot"
    $recoveryStarted = Get-Date
    & $restoreScript -BackupDirectory $backupDirectory -ProjectName $projectName -ComposeFiles $composeFiles -ConfirmRestore -KeepServicesStopped
    Invoke-Compose -Arguments @("up", "-d", "postgres", "minio")
    Wait-ForInfrastructure

    Write-Host "==> Verifying database, object data and migration revision"
    $verifyOutput = Invoke-ProbePython -Code $verifyCode -Environment $probeEnvironment
    $verification = (($verifyOutput | Select-Object -Last 1) | ConvertFrom-Json)
    $recoverySeconds = [Math]::Round(((Get-Date) - $recoveryStarted).TotalSeconds, 3)

    $checks.postgres_snapshot_restored = [int]$verification.seed_count -eq 1
    $checks.post_backup_database_change_removed = [int]$verification.post_count -eq 0
    $checks.minio_object_restored = [string]$verification.seed_content -ceq $seedContent
    $checks.post_backup_object_removed = [bool]$verification.post_object_absent
    $checks.current_migration_applied = -not [string]::IsNullOrWhiteSpace([string]$verification.revision)
    $checks.queued_task_state_restored = (
        [string]$verification.task_status -ceq "queued" -and
        [int]$verification.task_progress -eq 0 -and
        (@($verification.task_result.PSObject.Properties).Count -eq 0)
    )
    $failedChecks = @($checks.GetEnumerator() | Where-Object { -not $_.Value })
    if ($failedChecks.Count -gt 0) {
        throw "Recovery verification failed: $($failedChecks.Name -join ', ')"
    }

    Write-Host "Backup manifest tamper protection: PASS"
    Write-Host "Backup missing-file and PostgreSQL dump tamper protection: PASS"
    Write-Host "PostgreSQL snapshot restore: PASS"
    Write-Host "MinIO object restore: PASS"
    Write-Host "Queued background-task state restore: PASS"
    Write-Host "Backup/restore recovery drill passed. RTO=${recoverySeconds}s"
}
catch {
    $failure = $_.Exception.Message
    throw
}
finally {
    Write-Host "==> Removing only the isolated recovery Compose project and its volumes"
    try {
        if ($projectName -notmatch '^job-hunting-agent-recovery-[a-f0-9]{12}$') {
            throw "Refusing cleanup for unexpected project name: $projectName"
        }
        Invoke-Compose -Arguments @("down", "-v", "--remove-orphans")
        $cleanupSucceeded = $true
    }
    catch {
        Write-Warning "Isolated recovery cleanup failed: $($_.Exception.Message)"
    }

    $completedAt = (Get-Date).ToUniversalTime()
    $report = [ordered]@{
        drill_id = $drillId
        result = if ($failure) { "FAILED" } else { "PASSED" }
        started_at = $startedAt.ToString("o")
        completed_at = $completedAt.ToString("o")
        backup_seconds = $backupSeconds
        recovery_time_objective_observed_seconds = $recoverySeconds
        controlled_snapshot_data_loss_records = if ($failure) { $null } else { 0 }
        operational_rpo_measured = $false
        backup_directory = $backupDirectory
        checks = $checks
        isolated_project_cleanup = $cleanupSucceeded
        error = $failure
        note = "This controlled drill validates snapshot integrity and restore behavior; production RPO still depends on backup frequency."
    }
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Write-Host "Recovery report: $reportPath"
}
