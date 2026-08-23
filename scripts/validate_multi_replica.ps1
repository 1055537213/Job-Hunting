param(
    [ValidateRange(2, 8)]
    [int]$Replicas = 2,
    [ValidateRange(30, 300)]
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$Root = Split-Path -Parent $PSScriptRoot
$BaseCompose = Join-Path $Root "compose.yaml"
$ScaleCompose = Join-Path $Root "compose.scale-test.yaml"
$DevCompose = Join-Path $Root "compose.dev.yaml"
$ScaleFiles = @("-f", $BaseCompose, "-f", $ScaleCompose)
$RestoreFiles = @("-f", $BaseCompose, "-f", $DevCompose)
$RestoreRequired = $false

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

function Get-WebContainerIds {
    $ids = @(& docker compose @ScaleFiles ps -q web)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to list scaled Web containers."
    }
    return @($ids | Where-Object { $_ })
}

function Wait-ForWebReplicas {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $ids = Get-WebContainerIds
        if ($ids.Count -eq $Replicas) {
            $healthy = $true
            foreach ($id in $ids) {
                $status = (& docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" $id).Trim()
                if ($LASTEXITCODE -ne 0 -or $status -ne "healthy") {
                    $healthy = $false
                    break
                }
            }
            if ($healthy) {
                return $ids
            }
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    throw "Timed out waiting for $Replicas healthy Web replicas."
}

function Get-ContainerIp {
    param([string]$ContainerId)

    $address = (& docker inspect --format "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}" $ContainerId).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $address) {
        throw "Unable to resolve the container IP for $ContainerId."
    }
    return $address
}

function Wait-ForPrometheusTargets {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastSummary = "Prometheus has not returned its active target list yet."
    do {
        try {
            # Query current discovery state instead of the `up` time series. Prometheus keeps
            # removed targets as stale samples briefly, which can inflate a count after scaling.
            $targetsResponse = Invoke-RestMethod "http://127.0.0.1:9090/api/v1/targets"
            $webTargets = @(
                $targetsResponse.data.activeTargets |
                    Where-Object { $_.labels.job -eq "job-hunting-agent-web" }
            )
            $healthyTargets = @($webTargets | Where-Object { $_.health -eq "up" })
            $instances = @(
                $healthyTargets |
                    ForEach-Object { $_.labels.instance } |
                    Where-Object { $_ } |
                    Select-Object -Unique
            )
            $lastSummary = (
                "active=$($webTargets.Count), healthy=$($healthyTargets.Count), " +
                "distinct_instances=$($instances.Count)"
            )
            if (
                $webTargets.Count -eq $Replicas -and
                $healthyTargets.Count -eq $Replicas -and
                $instances.Count -eq $Replicas
            ) {
                return
            }
        }
        catch {
            # Prometheus may still be restarting or waiting for its first DNS refresh.
            $lastSummary = $_.Exception.Message
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    throw "Prometheus did not discover $Replicas healthy Web targets before timeout: $lastSummary"
}

$ProbeCode = @'
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import replace

from job_hunting_agent.config import load_concurrency_settings
from job_hunting_agent.concurrency_control import (
    ConcurrencyLimitExceeded,
    RedisConcurrencyController,
)

if len(sys.argv) > 1 and sys.argv[1] in {"hold", "check"}:
    settings = load_concurrency_settings("/app/.env", environ=os.environ)
    settings = replace(
        settings,
        key_prefix=os.environ["JOB_AGENT_CONCURRENCY_KEY_PREFIX"],
        model_global_limit=1,
        model_account_limit=1,
        lease_ttl_seconds=30,
        wait_timeout_seconds=0,
    )
    controller = RedisConcurrencyController(settings)
    if sys.argv[1] == "hold":
        lease = controller.acquire("model", account_id=1)
        print("READY", flush=True)
        time.sleep(8)
        lease.release()
    else:
        try:
            lease = controller.acquire("model", account_id=2)
        except ConcurrencyLimitExceeded:
            print("REJECTED", flush=True)
        else:
            lease.release()
            raise SystemExit("shared model lease was not enforced")
    raise SystemExit(0)

targets = [item for item in os.environ["TARGET_IPS"].split(",") if item]
expected_replicas = int(os.environ["EXPECTED_REPLICAS"])
auth_limit = int(os.environ["AUTH_LIMIT"])
assert len(targets) == expected_replicas, targets

for address in targets:
    with urllib.request.urlopen(f"http://{address}:8000/api/health", timeout=5) as response:
        assert response.status == 200
    with urllib.request.urlopen(f"http://{address}:8000/internal/metrics", timeout=5) as response:
        metrics = response.read().decode("utf-8")
        assert "job_agent_http_requests_total" in metrics

payload = json.dumps(
    {"email": "scale-probe@example.invalid", "password": "scale-probe-password"}
).encode("utf-8")
statuses = []
for index in range(auth_limit + 1):
    address = targets[index % len(targets)]
    request = urllib.request.Request(
        f"http://{address}:8000/api/auth/login",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            statuses.append(response.status)
    except urllib.error.HTTPError as error:
        statuses.append(error.code)

assert statuses[:auth_limit] == [401] * auth_limit, statuses
assert statuses[-1] == 429, statuses
print(f"Direct Web probes: {len(targets)} replicas; shared Redis rate limit: PASS")
'@
$ProbeRunner = "import os,base64;exec(base64.b64decode(os.environ['JOB_AGENT_SCALE_PROBE_B64']))"
$ProbeCodeB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($ProbeCode))

try {
    Write-Host "==> Starting $Replicas temporary Web replicas"
    Invoke-Compose -Files $ScaleFiles -Arguments @(
        "up", "-d", "--no-build", "--scale", "web=$Replicas", "web", "worker", "prometheus"
    )
    $RestoreRequired = $true

    $containerIds = Wait-ForWebReplicas
    $addresses = @($containerIds | ForEach-Object { Get-ContainerIp $_ })
    if (@($addresses | Select-Object -Unique).Count -ne $Replicas) {
        throw "Scaled Web containers did not receive distinct network addresses."
    }

    Write-Host "==> Probing every Web replica and the shared Redis limiter"
    & docker compose @ScaleFiles exec -T `
        -e "JOB_AGENT_SCALE_PROBE_B64=$ProbeCodeB64" `
        -e "TARGET_IPS=$($addresses -join ',')" `
        -e "EXPECTED_REPLICAS=$Replicas" `
        -e "AUTH_LIMIT=4" `
        worker python -c $ProbeRunner
    if ($LASTEXITCODE -ne 0) {
        throw "Multi-replica HTTP probe failed with exit code $LASTEXITCODE."
    }

    Write-Host "==> Probing shared Redis model concurrency lease across Web replicas"
    $probePrefix = "job_agent:scale_probe:$([guid]::NewGuid().ToString('N'))"
    $holderJob = Start-Job -ScriptBlock {
        param($containerId, $probeCodeB64, $probeRunner, $prefix)
        & docker exec `
            -e "JOB_AGENT_SCALE_PROBE_B64=$probeCodeB64" `
            -e "JOB_AGENT_CONCURRENCY_KEY_PREFIX=$prefix" `
            $containerId python -c $probeRunner hold
    } -ArgumentList $containerIds[0], $ProbeCodeB64, $ProbeRunner, $probePrefix
    try {
        $holderReady = $false
        $holderOutput = @()
        $holderDeadline = (Get-Date).AddSeconds(10)
        do {
            $holderOutput = @(Receive-Job -Job $holderJob -Keep)
            if ($holderOutput -contains "READY") {
                $holderReady = $true
                break
            }
            if ($holderJob.State -in @("Failed", "Stopped", "Completed")) {
                break
            }
            Start-Sleep -Milliseconds 250
        } while ((Get-Date) -lt $holderDeadline)
        if (-not $holderReady) {
            throw "First Web replica did not acquire the shared model lease: $($holderOutput -join ' ')"
        }
        $checkerOutput = & docker exec `
            -e "JOB_AGENT_SCALE_PROBE_B64=$ProbeCodeB64" `
            -e "JOB_AGENT_CONCURRENCY_KEY_PREFIX=$probePrefix" `
            $containerIds[1] python -c $ProbeRunner check
        if ($LASTEXITCODE -ne 0 -or $checkerOutput -notcontains "REJECTED") {
            throw "Second Web replica did not observe the shared model lease: $($checkerOutput -join ' ')"
        }
        Write-Host "Shared Redis model concurrency lease: PASS"
    }
    finally {
        Stop-Job -Job $holderJob -ErrorAction SilentlyContinue
        Remove-Job -Job $holderJob -Force -ErrorAction SilentlyContinue
    }

    Write-Host "==> Restarting Prometheus and verifying DNS service discovery"
    Invoke-Compose -Files $ScaleFiles -Arguments @("restart", "prometheus")
    Wait-ForPrometheusTargets
    Write-Host "Multi-replica validation passed: $Replicas Web targets are healthy."
}
finally {
    if ($RestoreRequired) {
        Write-Host "==> Restoring the normal single-Web development topology"
        try {
            Invoke-Compose -Files $RestoreFiles -Arguments @(
                "up", "-d", "--no-build", "--scale", "web=1", "web", "worker", "prometheus"
            )
        }
        catch {
            Write-Warning "Automatic topology restore failed: $($_.Exception.Message)"
        }
    }
}
