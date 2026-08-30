[CmdletBinding()]
param(
    [string]$Image = "job-hunting-agent:alert-acceptance",
    [ValidateRange(30, 300)]
    [int]$TimeoutSeconds = 180,
    [string]$EvidenceRoot = "data/observability-drills",
    [switch]$SkipBuild,
    [switch]$KeepEnvironment
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$Root = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $Root "compose.observability-test.yaml"
$Suffix = [guid]::NewGuid().ToString("N").Substring(0, 12)
$RunId = "alert-delivery-$Suffix"
$ProjectName = "job-agent-alert-$Suffix"
$EvidenceBase = if ([IO.Path]::IsPathRooted($EvidenceRoot)) {
    [IO.Path]::GetFullPath($EvidenceRoot)
}
else {
    [IO.Path]::GetFullPath((Join-Path $Root $EvidenceRoot))
}
$RunDirectory = Join-Path $EvidenceBase ((Get-Date -Format "yyyyMMdd-HHmmss") + "-$Suffix")
$RuntimeDirectory = Join-Path $RunDirectory "runtime"
$ReportPath = Join-Path $RunDirectory "alert-delivery-report.json"
$AlertmanagerConfig = Join-Path $RuntimeDirectory "alertmanager.yml"
$PrometheusConfig = Join-Path $RuntimeDirectory "prometheus.yml"
$AlertRules = Join-Path $RuntimeDirectory "alert-rules.yml"
$Recipient = "local-alert@example.invalid"
$AlertName = "JobAgentLocalAlertDeliveryAcceptance"
$FailureMessage = $null
$FiringResult = $null
$ResolvedResult = $null
$CleanupSucceeded = $false
$TestsPassed = $false

$env:JOB_AGENT_OBSERVABILITY_ACCEPTANCE_IMAGE = $Image
$env:JOB_AGENT_OBSERVABILITY_RUNTIME_DIR = $RuntimeDirectory.Replace("\", "/")
$env:JOB_AGENT_MAILPIT_IMAGE = "axllent/mailpit:v1.30.6@sha256:7f33095f80e901f6ad08028f06ca284aa58fe84942be5496008d041d3b9f4d4d"
$ComposeArgs = @("compose", "--project-name", $ProjectName, "-f", $ComposeFile)

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Invoke-Compose {
    param([Parameter(Mandatory)][string[]]$Arguments)

    Invoke-Docker -Arguments ($ComposeArgs + $Arguments)
}

function Invoke-ProbePython {
    param(
        [Parameter(Mandatory)][string]$Code,
        [hashtable]$Environment = @{}
    )

    $CodeB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
    $Arguments = $ComposeArgs + @(
        "run", "--rm", "--no-deps",
        "-e", "JOB_AGENT_ALERT_PROBE_CODE_B64=$CodeB64"
    )
    foreach ($Entry in $Environment.GetEnumerator()) {
        $Arguments += @("-e", "$($Entry.Key)=$($Entry.Value)")
    }
    $Arguments += @(
        "--entrypoint", "python", "probe", "-c",
        "import base64,os;exec(base64.b64decode(os.environ['JOB_AGENT_ALERT_PROBE_CODE_B64']))"
    )
    $Output = @(& docker @Arguments)
    if ($LASTEXITCODE -ne 0) {
        throw "Alert-delivery probe failed with exit code $LASTEXITCODE."
    }
    return (($Output | Select-Object -Last 1) | ConvertFrom-Json)
}

function Write-AlertRules {
    param([Parameter(Mandatory)][string]$Expression)

    @"
groups:
  - name: local-alert-delivery-acceptance
    interval: 1s
    rules:
      - alert: $AlertName
        expr: $Expression
        for: 0s
        labels:
          severity: warning
        annotations:
          summary: Local Alertmanager SMTP delivery acceptance
"@ | Set-Content -LiteralPath $AlertRules -Encoding UTF8
}

$GenerateConfigCode = @'
import json
import os
from pathlib import Path

from job_hunting_agent.observability_config import (
    AlertmanagerSettings,
    render_alertmanager_config,
)

settings = AlertmanagerSettings(
    smtp_host="mailpit",
    smtp_port=1025,
    smtp_username="acceptance-user",
    smtp_password="acceptance-password",
    smtp_from_email="alerts@example.invalid",
    alert_email_to="local-alert@example.invalid",
)
config = json.loads(render_alertmanager_config(settings, smtp_require_tls=False))
config["route"]["group_wait"] = "1s"
config["route"]["group_interval"] = "1s"
config["route"]["repeat_interval"] = "1h"
Path("/runtime/alertmanager.yml").write_text(
    json.dumps(config, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
'@

$WaitForMessageCode = @'
import json
import os
import time
import urllib.request

timeout = int(os.environ["JOB_AGENT_ALERT_PROBE_TIMEOUT"])
expected_state = os.environ["JOB_AGENT_ALERT_EXPECTED_STATE"]
alert_name = os.environ["JOB_AGENT_ALERT_NAME"]
recipient = os.environ["JOB_AGENT_ALERT_RECIPIENT"]
deadline = time.monotonic() + timeout
last_error = ""
while time.monotonic() < deadline:
    try:
        for url in (
            "http://mailpit:8025/api/v1/messages?limit=20",
            "http://alertmanager:9093/-/ready",
            "http://prometheus:9090/-/ready",
        ):
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status != 200:
                    raise RuntimeError(f"unexpected status {response.status}: {url}")
                body = response.read()
                if "messages" in url:
                    messages = json.loads(body).get("messages", [])
        matching = []
        for message in messages:
            subject = str(message.get("Subject", ""))
            recipients = json.dumps(message.get("To", []), ensure_ascii=False)
            if alert_name in subject and expected_state in subject and recipient in recipients:
                matching.append({"id": message.get("ID"), "subject": subject})
        if matching:
            print(json.dumps({
                "state": expected_state,
                "message_count": len(messages),
                "matching": matching,
            }, ensure_ascii=False))
            raise SystemExit(0)
    except Exception as error:
        last_error = f"{type(error).__name__}: {error}"
    time.sleep(1)
raise SystemExit(f"Timed out waiting for {expected_state} alert email: {last_error}")
'@

$ReloadPrometheusCode = @'
import urllib.request

request = urllib.request.Request(
    "http://prometheus:9090/-/reload",
    data=b"",
    method="POST",
)
with urllib.request.urlopen(request, timeout=5) as response:
    if response.status != 200:
        raise SystemExit(f"Prometheus reload failed: {response.status}")
print('{"reloaded":true}')
'@

New-Item -ItemType Directory -Force -Path $RuntimeDirectory | Out-Null

try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "docker command was not found. Start Docker Desktop and try again."
    }
    if ([string]::IsNullOrWhiteSpace($Image)) {
        throw "Image must not be empty."
    }

    Write-Host "==> Preparing the isolated alert-delivery acceptance stack"
    Invoke-Docker -Arguments @("version")
    if (-not $SkipBuild) {
        Invoke-Docker -Arguments @("build", "--pull", "--tag", $Image, $Root)
    }
    else {
        $null = & docker image inspect $Image
        if ($LASTEXITCODE -ne 0) {
            throw "The requested image does not exist: $Image"
        }
    }

    $RuntimeMount = "$($RuntimeDirectory.Replace('\', '/')):/runtime"
    Invoke-Docker -Arguments @(
        "run", "--rm", "-v", $RuntimeMount,
        "--entrypoint", "python", $Image, "-c",
        "import base64;exec(base64.b64decode('$([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($GenerateConfigCode)))'))"
    )
    @"
global:
  evaluation_interval: 1s
alerting:
  alertmanagers:
    - static_configs:
        - targets: ["alertmanager:9093"]
rule_files:
  - /etc/prometheus/alert-rules.yml
scrape_configs: []
"@ | Set-Content -LiteralPath $PrometheusConfig -Encoding UTF8
    Write-AlertRules -Expression "vector(1) == 1"
    Invoke-Compose -Arguments @("config", "--quiet")

    Write-Host "==> Starting isolated Prometheus, Alertmanager and Mailpit"
    Invoke-Compose -Arguments @("up", "-d", "mailpit", "alertmanager", "prometheus")

    Write-Host "==> Waiting for the FIRING email captured through SMTP"
    $FiringResult = Invoke-ProbePython -Code $WaitForMessageCode -Environment @{
        JOB_AGENT_ALERT_PROBE_TIMEOUT = [string]$TimeoutSeconds
        JOB_AGENT_ALERT_EXPECTED_STATE = "FIRING"
        JOB_AGENT_ALERT_NAME = $AlertName
        JOB_AGENT_ALERT_RECIPIENT = $Recipient
    }

    Write-Host "==> Resolving the rule and waiting for the RESOLVED email"
    Write-AlertRules -Expression "vector(0) == 1"
    $null = Invoke-ProbePython -Code $ReloadPrometheusCode
    $ResolvedResult = Invoke-ProbePython -Code $WaitForMessageCode -Environment @{
        JOB_AGENT_ALERT_PROBE_TIMEOUT = [string]$TimeoutSeconds
        JOB_AGENT_ALERT_EXPECTED_STATE = "RESOLVED"
        JOB_AGENT_ALERT_NAME = $AlertName
        JOB_AGENT_ALERT_RECIPIENT = $Recipient
    }

    Write-Host "Prometheus firing and resolution: PASS"
    Write-Host "Alertmanager SMTP firing and resolved delivery: PASS"
    $TestsPassed = $true
}
catch {
    $FailureMessage = $_.Exception.Message
    throw
}
finally {
    if ($KeepEnvironment) {
        Write-Warning "Keeping isolated alert project for inspection: $ProjectName"
    }
    else {
        try {
            Invoke-Compose -Arguments @("down", "-v", "--remove-orphans")
            $CleanupSucceeded = $true
        }
        catch {
            Write-Warning "Automatic alert acceptance cleanup failed: $($_.Exception.Message)"
        }
    }

    $Report = [ordered]@{
        run_id = $RunId
        result = if ($TestsPassed -and [string]::IsNullOrWhiteSpace($FailureMessage)) { "PASSED" } else { "FAILED" }
        generated_at = [DateTime]::UtcNow.ToString("o")
        project_name = $ProjectName
        alert_name = $AlertName
        recipient = $Recipient
        firing = $FiringResult
        resolved = $ResolvedResult
        isolated_project_cleanup = $CleanupSucceeded
        tests_passed = $TestsPassed
        error = $FailureMessage
        note = "Mailpit is isolated and test-only; production SMTP still requires TLS and a real recipient acceptance test."
    }
    $Report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    Write-Host "Alert-delivery report: $ReportPath"
}
