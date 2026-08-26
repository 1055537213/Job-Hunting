param(
    [ValidateRange(5, 25)]
    [int]$ProbeDelaySeconds = 20,
    [ValidateRange(120, 600)]
    [int]$TimeoutSeconds = 240
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$Root = Split-Path -Parent $PSScriptRoot
$BaseCompose = Join-Path $Root "compose.yaml"
$DevCompose = Join-Path $Root "compose.dev.yaml"
$AcceptanceCompose = Join-Path $Root "compose.acceptance.yaml"
$ComposeFiles = @("-f", $BaseCompose, "-f", $DevCompose, "-f", $AcceptanceCompose)
$NormalComposeFiles = @("-f", $BaseCompose, "-f", $DevCompose)
$RestoreRequired = $false
$WorkerContainerId = $null
$MaintenanceWorkerContainerId = $null
$MaintenanceQueue = $null
$AccountId = $null
$TaskKey = $null

function Invoke-Compose {
    param(
        [string[]]$Files,
        [string[]]$Arguments
    )

    & docker compose @Files @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Invoke-ComposeOutput {
    param(
        [string[]]$Files,
        [string[]]$Arguments
    )

    $output = @(& docker compose @Files @Arguments)
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
    return $output
}

function Invoke-Docker {
    param([string[]]$Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Get-WorkerContainerId {
    $ids = @(
        Invoke-ComposeOutput -Files $ComposeFiles -Arguments @("ps", "-q", "worker") |
            ForEach-Object { $_.ToString().Trim() } |
            Where-Object { $_ }
    )
    if ($ids.Count -ne 1) {
        throw "Expected exactly one Worker container, found $($ids.Count)."
    }
    return $ids[0]
}

function Get-ContainerStatus {
    param([string]$ContainerId)

    return (& docker inspect --format "{{.State.Status}}" $ContainerId).Trim()
}

function Invoke-WebPython {
    param(
        [string]$Code,
        [hashtable]$Environment = @{}
    )

    $codeB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
    $arguments = @("exec", "-T", "-e", "JOB_AGENT_ACCEPTANCE_CODE_B64=$codeB64")
    foreach ($entry in $Environment.GetEnumerator()) {
        $arguments += @("-e", "$($entry.Key)=$($entry.Value)")
    }
    $arguments += @(
        "web",
        "python",
        "-c",
        "import base64,os;exec(base64.b64decode(os.environ['JOB_AGENT_ACCEPTANCE_CODE_B64']))"
    )
    $output = @(& docker compose @ComposeFiles @arguments)
    if ($LASTEXITCODE -ne 0) {
        throw "The Web-side acceptance probe failed with exit code $LASTEXITCODE."
    }
    return $output
}

function Get-ProbeState {
    $stateCode = @'
import json
import os

from job_hunting_agent.app import JobHuntingApp

backend = JobHuntingApp(env_path="/app/.env")
try:
    record = backend.store.get_background_task(os.environ["JOB_AGENT_ACCEPTANCE_TASK_KEY"])
    print(json.dumps({
        "task_key": record.task_key,
        "status": record.status,
        "attempt": record.attempt,
        "progress": record.progress,
        "error_summary": record.error_summary,
        "result": record.result,
    }, ensure_ascii=False))
finally:
    backend.store.close()
'@
    $output = Invoke-WebPython -Code $stateCode -Environment @{
        JOB_AGENT_ACCEPTANCE_TASK_KEY = $TaskKey
    }
    return (($output -join "`n") | ConvertFrom-Json)
}

$BootstrapCode = @'
import json
import os

from job_hunting_agent.app import JobHuntingApp

backend = JobHuntingApp(env_path="/app/.env")
try:
    backend.initialize()
    account = backend.store.create_account(
        os.environ["JOB_AGENT_ACCEPTANCE_EMAIL"],
        "worker-recovery-acceptance-password-hash",
        display_name="Worker recovery acceptance",
    )
    task = backend.enqueue_background_task(
        account_id=account.id,
        task_type="system_probe",
        payload={
            "purpose": "worker_recovery_acceptance",
            "delay_seconds": int(os.environ["JOB_AGENT_ACCEPTANCE_DELAY_SECONDS"]),
        },
        idempotency_key=os.environ["JOB_AGENT_ACCEPTANCE_IDEMPOTENCY_KEY"],
        max_attempts=2,
    )
    duplicate = backend.enqueue_background_task(
        account_id=account.id,
        task_type="system_probe",
        payload={
            "purpose": "worker_recovery_acceptance",
            "delay_seconds": int(os.environ["JOB_AGENT_ACCEPTANCE_DELAY_SECONDS"]),
        },
        idempotency_key=os.environ["JOB_AGENT_ACCEPTANCE_IDEMPOTENCY_KEY"],
        max_attempts=2,
    )
    with backend.store.connect() as conn:
        counts = {}
        for table in (
            "background_tasks",
            "usage_events",
            "account_balance_ledger",
            "resume_artifacts",
            "tool_call_traces",
        ):
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE account_id = ?",
                (account.id,),
            ).fetchone()
            counts[table] = int(row["count"])
    assert task.task_key == duplicate.task_key
    assert task.id == duplicate.id
    print(json.dumps({
        "account_id": account.id,
        "task_key": task.task_key,
        "duplicate_task_key": duplicate.task_key,
        "maintenance_queue": f"{backend.task_queue_settings.queue_name}_maintenance",
        "initial_status": task.status,
        "initial_counts": counts,
    }, ensure_ascii=False))
finally:
    backend.store.close()
'@

$DuplicateCode = @'
import json
import os

from job_hunting_agent.app import JobHuntingApp

backend = JobHuntingApp(env_path="/app/.env")
try:
    duplicate = backend.enqueue_background_task(
        account_id=int(os.environ["JOB_AGENT_ACCEPTANCE_ACCOUNT_ID"]),
        task_type="system_probe",
        payload={
            "purpose": "worker_recovery_acceptance",
            "delay_seconds": int(os.environ["JOB_AGENT_ACCEPTANCE_DELAY_SECONDS"]),
        },
        idempotency_key=os.environ["JOB_AGENT_ACCEPTANCE_IDEMPOTENCY_KEY"],
        max_attempts=2,
    )
    with backend.store.connect() as conn:
        counts = {}
        for table in (
            "background_tasks",
            "usage_events",
            "account_balance_ledger",
            "resume_artifacts",
            "tool_call_traces",
        ):
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE account_id = ?",
                (int(os.environ["JOB_AGENT_ACCEPTANCE_ACCOUNT_ID"]),),
            ).fetchone()
            counts[table] = int(row["count"])
    print(json.dumps({
        "task_key": duplicate.task_key,
        "status": duplicate.status,
        "counts": counts,
    }, ensure_ascii=False))
finally:
    backend.store.close()
'@

$CleanupCode = @'
import os

from job_hunting_agent.app import JobHuntingApp

backend = JobHuntingApp(env_path="/app/.env")
try:
    with backend.store.connect() as conn:
        conn.execute(
            "DELETE FROM accounts WHERE id = ?",
            (int(os.environ["JOB_AGENT_ACCEPTANCE_ACCOUNT_ID"]),),
        )
finally:
    backend.store.close()
'@

try {
    Write-Host "==> Checking Docker and acceptance Compose configuration"
    Invoke-Docker -Arguments @("version")
    Invoke-Compose -Files $ComposeFiles -Arguments @("config", "--quiet")

    Write-Host "==> Starting infrastructure and applying the current database migrations"
    Invoke-Compose -Files $ComposeFiles -Arguments @("up", "-d", "postgres", "redis", "minio")
    Invoke-Compose -Files $ComposeFiles -Arguments @("run", "--rm", "migrate")

    Write-Host "==> Starting Web, Worker and Beat with short recovery windows"
    Invoke-Compose -Files $ComposeFiles -Arguments @(
        "up", "-d", "--no-build", "--force-recreate", "web", "worker", "beat"
    )
    $RestoreRequired = $true
    $WorkerContainerId = Get-WorkerContainerId

    $suffix = [guid]::NewGuid().ToString("N")
    $email = "worker-recovery-$suffix@example.invalid"
    $idempotencyKey = "worker-recovery-$suffix"
    $bootstrapOutput = Invoke-WebPython -Code $BootstrapCode -Environment @{
        JOB_AGENT_ACCEPTANCE_EMAIL = $email
        JOB_AGENT_ACCEPTANCE_DELAY_SECONDS = $ProbeDelaySeconds
        JOB_AGENT_ACCEPTANCE_IDEMPOTENCY_KEY = $idempotencyKey
    }
    $bootstrap = (($bootstrapOutput -join "`n") | ConvertFrom-Json)
    $AccountId = [int]$bootstrap.account_id
    $TaskKey = [string]$bootstrap.task_key
    $MaintenanceQueue = [string]$bootstrap.maintenance_queue
    if ($bootstrap.duplicate_task_key -ne $TaskKey) {
        throw "The initial idempotency probe returned two task keys."
    }
    Write-Host "Created one idempotent system probe: $TaskKey"

    Write-Host "==> Waiting for the Worker to claim the probe"
    $runningDeadline = (Get-Date).AddSeconds([Math]::Min(60, $TimeoutSeconds))
    do {
        $state = Get-ProbeState
        if ($state.status -eq "running") {
            break
        }
        if ($state.status -in @("succeeded", "failed", "cancelled")) {
            throw "Probe reached terminal state before the Worker failure test: $($state.status)."
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $runningDeadline)
    if ($state.status -ne "running") {
        throw "Timed out waiting for the probe to enter running state."
    }

    Write-Host "==> Starting a maintenance-only Worker so Beat remains available"
    $maintenanceOutput = Invoke-Compose -Files $ComposeFiles -Arguments @(
        "run", "-d", "--no-deps", "--name", "job-hunting-agent-worker-recovery", "worker",
        "job-agent-worker", "--env-file", "/app/.env", "--log-level", "INFO",
        "--concurrency", "1", "--queue", $MaintenanceQueue
    )
    $MaintenanceWorkerContainerId = (($maintenanceOutput -join "`n").Trim() -split "`r?`n")[-1].Trim()
    if (-not $MaintenanceWorkerContainerId) {
        throw "The maintenance-only Worker did not return a container ID."
    }

    Write-Host "==> Forcing the Worker container down while the task is running"
    # Disable the restart policy first so the stale database state remains observable until Beat
    # performs its recovery pass. The original topology is restored in finally.
    Invoke-Docker -Arguments @("update", "--restart=no", $WorkerContainerId)
    Invoke-Docker -Arguments @("kill", "--signal", "KILL", $WorkerContainerId)
    $stoppedDeadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Seconds 1
        $containerStatus = Get-ContainerStatus $WorkerContainerId
    } while ($containerStatus -notin @("exited", "dead") -and (Get-Date) -lt $stoppedDeadline)
    if ($containerStatus -notin @("exited", "dead")) {
        throw "Worker container did not stop after SIGKILL."
    }

    Write-Host "==> Waiting for Beat to requeue the stale task"
    $recoveryDeadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $state = Get-ProbeState
        if ($state.status -eq "queued") {
            break
        }
        if ($state.status -in @("succeeded", "failed", "cancelled")) {
            throw "Probe reached terminal state before Beat recovery: $($state.status)."
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $recoveryDeadline)
    if ($state.status -ne "queued" -or [int]$state.attempt -lt 1) {
        throw "Beat did not requeue the stale task with an existing attempt: $($state | ConvertTo-Json -Compress)"
    }
    Write-Host "Beat stale-task recovery: PASS (status=queued, attempt=$($state.attempt))"

    Write-Host "==> Starting the stopped Worker and waiting for one successful completion"
    Invoke-Docker -Arguments @("start", $WorkerContainerId)
    Invoke-Docker -Arguments @("update", "--restart=unless-stopped", $WorkerContainerId)
    $completionDeadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Seconds 2
        $state = Get-ProbeState
        if ($state.status -in @("succeeded", "failed", "cancelled")) {
            break
        }
    } while ((Get-Date) -lt $completionDeadline)
    if ($state.status -ne "succeeded") {
        throw "Recovered task did not complete successfully: $($state | ConvertTo-Json -Compress)"
    }
    if ($state.result.worker -ne "ready") {
        throw "Recovered probe result did not come from the Worker."
    }
    Write-Host "Recovered task completion: PASS"

    Write-Host "==> Verifying duplicate delivery and no duplicate billing/file side effects"
    $duplicateOutput = Invoke-WebPython -Code $DuplicateCode -Environment @{
        JOB_AGENT_ACCEPTANCE_ACCOUNT_ID = $AccountId
        JOB_AGENT_ACCEPTANCE_DELAY_SECONDS = $ProbeDelaySeconds
        JOB_AGENT_ACCEPTANCE_IDEMPOTENCY_KEY = $idempotencyKey
    }
    $duplicate = (($duplicateOutput -join "`n") | ConvertFrom-Json)
    if ($duplicate.task_key -ne $TaskKey -or $duplicate.status -ne "succeeded") {
        throw "Duplicate idempotency request did not reuse the successful task."
    }
    $counts = $duplicate.counts
    if ([int]$counts.background_tasks -ne 1 -or [int]$counts.usage_events -ne 0 -or
        [int]$counts.account_balance_ledger -ne 0 -or [int]$counts.resume_artifacts -ne 0 -or
        [int]$counts.tool_call_traces -ne 1) {
        throw "Recovery changed side-effect counts unexpectedly: $($duplicate | ConvertTo-Json -Compress)"
    }
    Write-Host "Idempotency and side-effect check: PASS"
    Write-Host "Worker recovery acceptance passed."
}
finally {
    if ($RestoreRequired) {
        if ($MaintenanceWorkerContainerId) {
            Write-Host "==> Removing the temporary maintenance-only Worker"
            try {
                Invoke-Docker -Arguments @("rm", "-f", $MaintenanceWorkerContainerId)
            }
            catch {
                Write-Warning "Could not remove the maintenance-only Worker: $($_.Exception.Message)"
            }
        }
        Write-Host "==> Stopping the acceptance Worker before cleanup"
        try {
            Invoke-Compose -Files $ComposeFiles -Arguments @("stop", "worker")
        }
        catch {
            Write-Warning "Could not stop the acceptance Worker before cleanup: $($_.Exception.Message)"
        }

        if ($AccountId) {
            Write-Host "==> Removing the isolated acceptance account and its cascaded records"
            try {
                $null = Invoke-WebPython -Code $CleanupCode -Environment @{
                    JOB_AGENT_ACCEPTANCE_ACCOUNT_ID = $AccountId
                }
            }
            catch {
                Write-Warning "Acceptance data cleanup failed; inspect account $AccountId manually: $($_.Exception.Message)"
            }
        }

        Write-Host "==> Restoring the normal single-Web development topology"
        try {
            Invoke-Compose -Files $NormalComposeFiles -Arguments @(
                "up", "-d", "--no-build", "--scale", "web=1", "web", "worker", "beat"
            )
        }
        catch {
            Write-Warning "Automatic topology restore failed: $($_.Exception.Message)"
        }
    }
}
