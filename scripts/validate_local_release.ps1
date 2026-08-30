[CmdletBinding()]
param(
    [string]$Image = "job-hunting-agent:local-release-acceptance",
    [string]$Python = "python",
    [string]$EvidenceRoot = "data/local-release-drills",
    [switch]$SkipBuild,
    [switch]$KeepEnvironments
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$Root = Split-Path -Parent $PSScriptRoot
$Suffix = [guid]::NewGuid().ToString("N").Substring(0, 12)
$EvidenceBase = if ([IO.Path]::IsPathRooted($EvidenceRoot)) {
    [IO.Path]::GetFullPath($EvidenceRoot)
}
else {
    [IO.Path]::GetFullPath((Join-Path $Root $EvidenceRoot))
}
$RunDirectory = Join-Path $EvidenceBase ((Get-Date -Format "yyyyMMdd-HHmmss") + "-$Suffix")
$ReportPath = Join-Path $RunDirectory "local-release-report.json"
$RecoveryEvidence = Join-Path $RunDirectory "recovery"
$FileScanEvidence = Join-Path $RunDirectory "file-scan"
$AlertEvidence = Join-Path $RunDirectory "alert-delivery"
$StartedAt = [DateTime]::UtcNow
$FailureMessage = $null
$TestsPassed = $false
$Steps = [ordered]@{
    upload_security = [ordered]@{ status = "PENDING" }
    image_build = [ordered]@{ status = "PENDING"; image = $Image }
    backup_restore = [ordered]@{ status = "PENDING" }
    file_scanning = [ordered]@{ status = "PENDING" }
    alert_delivery = [ordered]@{ status = "PENDING" }
}

function Get-LatestReport {
    param(
        [Parameter(Mandatory)][string]$Directory,
        [Parameter(Mandatory)][string]$Name
    )

    $Report = Get-ChildItem -LiteralPath $Directory -Filter $Name -File -Recurse |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if ($null -eq $Report) {
        throw "Expected report was not created: $Name"
    }
    return $Report.FullName
}

New-Item -ItemType Directory -Force -Path $RunDirectory | Out-Null

try {
    Write-Host "==> Running upload attack-boundary regression tests"
    & $Python -m pytest -q tests/test_upload_security.py
    if ($LASTEXITCODE -ne 0) {
        throw "Upload security tests failed with exit code $LASTEXITCODE."
    }
    $Steps.upload_security.status = "PASSED"

    Write-Host "==> Preparing one reusable application image"
    if ($SkipBuild) {
        & docker image inspect $Image | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "The requested image does not exist: $Image"
        }
    }
    else {
        & docker build --pull --tag $Image $Root
        if ($LASTEXITCODE -ne 0) {
            throw "Application image build failed with exit code $LASTEXITCODE."
        }
    }
    $Steps.image_build.status = "PASSED"

    Write-Host "==> Running isolated PostgreSQL and MinIO recovery drill"
    & (Join-Path $PSScriptRoot "validate_backup_restore.ps1") -EvidenceRoot $RecoveryEvidence
    $Steps.backup_restore.status = "PASSED"
    $Steps.backup_restore.report = Get-LatestReport -Directory $RecoveryEvidence -Name "recovery-report.json"

    Write-Host "==> Running production-configured ClamAV acceptance"
    $FileScanArguments = @{
        Image = $Image
        EvidenceRoot = $FileScanEvidence
        SkipBuild = $true
        KeepEnvironment = $KeepEnvironments
    }
    & (Join-Path $PSScriptRoot "validate_file_scanning.ps1") @FileScanArguments
    $Steps.file_scanning.status = "PASSED"
    $Steps.file_scanning.report = Get-LatestReport -Directory $FileScanEvidence -Name "file-scan-report.json"

    Write-Host "==> Running Prometheus, Alertmanager and SMTP delivery acceptance"
    $AlertArguments = @{
        Image = $Image
        EvidenceRoot = $AlertEvidence
        SkipBuild = $true
        KeepEnvironment = $KeepEnvironments
    }
    & (Join-Path $PSScriptRoot "validate_alert_delivery.ps1") @AlertArguments
    $Steps.alert_delivery.status = "PASSED"
    $Steps.alert_delivery.report = Get-LatestReport -Directory $AlertEvidence -Name "alert-delivery-report.json"

    Write-Host "Local release acceptance pack: PASS"
    $TestsPassed = $true
}
catch {
    $FailureMessage = $_.Exception.Message
    throw
}
finally {
    $CompletedAt = [DateTime]::UtcNow
    $Report = [ordered]@{
        run_id = "local-release-$Suffix"
        result = if ($TestsPassed -and [string]::IsNullOrWhiteSpace($FailureMessage)) { "PASSED" } else { "FAILED" }
        started_at = $StartedAt.ToString("o")
        completed_at = $CompletedAt.ToString("o")
        duration_seconds = [Math]::Round(($CompletedAt - $StartedAt).TotalSeconds, 3)
        application_image = $Image
        tests_passed = $TestsPassed
        steps = $Steps
        error = $FailureMessage
        note = "This isolated local pack does not replace production capacity, HTTPS, external backup, or real SMTP acceptance."
    }
    $Report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    Write-Host "Local release report: $ReportPath"
}
