[CmdletBinding()]
param(
    [string]$Image = "job-hunting-agent:security-scan",
    [string]$ReportRoot = "data/security-reports",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    # Vulnerability scanners use exit code 1 as a policy result. The script records both scans
    # before enforcing the combined gate, so native failures are handled explicitly below.
    $PSNativeCommandUseErrorActionPreference = $false
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$pythonAuditImage = "python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"
$pipAuditVersion = "2.10.1"
$trivyImage = "aquasec/trivy@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$reportBase = if ([IO.Path]::IsPathRooted($ReportRoot)) {
    [IO.Path]::GetFullPath($ReportRoot)
}
else {
    [IO.Path]::GetFullPath((Join-Path $repositoryRoot $ReportRoot))
}
$reportPath = Join-Path $reportBase $timestamp
$pythonReport = Join-Path $reportPath "python-dependencies.json"
$containerReport = Join-Path $reportPath "container-vulnerabilities.json"
$sbomReport = Join-Path $reportPath "image-sbom.cdx.json"
$summaryReport = Join-Path $reportPath "security-summary.json"
$reportDockerPath = $reportPath.Replace("\", "/")
$runtimeLockDockerPath = (Join-Path $repositoryRoot "requirements.lock").Replace("\", "/")
$developmentLockDockerPath = (Join-Path $repositoryRoot "requirements-dev.lock").Replace("\", "/")

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Get-PythonVulnerabilityCount {
    if (-not (Test-Path -LiteralPath $pythonReport -PathType Leaf)) {
        return $null
    }
    $report = Get-Content -Raw -LiteralPath $pythonReport | ConvertFrom-Json
    return @($report.dependencies | ForEach-Object { @($_.vulns) }).Count
}

function Get-ContainerVulnerabilityCounts {
    if (-not (Test-Path -LiteralPath $containerReport -PathType Leaf)) {
        return $null
    }
    $report = Get-Content -Raw -LiteralPath $containerReport | ConvertFrom-Json
    $vulnerabilities = @($report.Results | ForEach-Object { @($_.Vulnerabilities) })
    return [ordered]@{
        high = @($vulnerabilities | Where-Object { $_.Severity -eq "HIGH" }).Count
        critical = @($vulnerabilities | Where-Object { $_.Severity -eq "CRITICAL" }).Count
        fixable = @($vulnerabilities | Where-Object {
            $null -ne $_.PSObject.Properties["FixedVersion"] -and
            -not [string]::IsNullOrWhiteSpace([string]$_.FixedVersion)
        }).Count
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker command was not found. Start Docker Desktop and try again."
}
if ([string]::IsNullOrWhiteSpace($Image)) {
    throw "Image must not be empty."
}

New-Item -ItemType Directory -Force -Path $reportPath | Out-Null

Write-Host "==> Auditing locked Python dependencies with pip-audit $pipAuditVersion"
$pythonAuditArguments = @(
    "run", "--rm",
    "-e", "PIP_ROOT_USER_ACTION=ignore",
    "-v", "${runtimeLockDockerPath}:/workspace/requirements.lock:ro",
    "-v", "${developmentLockDockerPath}:/workspace/requirements-dev.lock:ro",
    "-v", "${reportDockerPath}:/reports",
    "-w", "/workspace",
    $pythonAuditImage,
    "sh", "-c",
    "python -m pip install --quiet --disable-pip-version-check pip-audit==$pipAuditVersion && python -m pip_audit -r requirements.lock -r requirements-dev.lock --format json --output /reports/python-dependencies.json"
)
& docker @pythonAuditArguments
$pythonAuditExitCode = $LASTEXITCODE

if (-not $SkipBuild) {
    Write-Host "==> Building the final runtime image from the pinned base digest"
    Invoke-Docker -Arguments @(
        "build",
        "--pull",
        "--no-cache",
        "--tag", $Image,
        $repositoryRoot
    )
}
else {
    Write-Host "==> Reusing existing image: $Image"
    $null = & docker image inspect $Image
    if ($LASTEXITCODE -ne 0) {
        throw "The requested image does not exist: $Image"
    }
}

Write-Host "==> Recording all HIGH/CRITICAL container findings"
Invoke-Docker -Arguments @(
    "run", "--rm",
    "-v", "/var/run/docker.sock:/var/run/docker.sock",
    "-v", "job-agent-trivy-cache:/root/.cache",
    "-v", "${reportDockerPath}:/reports",
    $trivyImage,
    "image", "--quiet", "--scanners", "vuln", "--pkg-types", "os",
    "--severity", "HIGH,CRITICAL",
    "--format", "json", "--output", "/reports/container-vulnerabilities.json", $Image
)

Write-Host "==> Generating a CycloneDX software bill of materials"
Invoke-Docker -Arguments @(
    "run", "--rm",
    "-v", "/var/run/docker.sock:/var/run/docker.sock",
    "-v", "job-agent-trivy-cache:/root/.cache",
    "-v", "${reportDockerPath}:/reports",
    $trivyImage,
    "image", "--quiet", "--format", "cyclonedx", "--output", "/reports/image-sbom.cdx.json", $Image
)

Write-Host "==> Enforcing the fixable HIGH/CRITICAL container gate"
$containerAuditArguments = @(
    "run", "--rm",
    "-v", "/var/run/docker.sock:/var/run/docker.sock",
    "-v", "job-agent-trivy-cache:/root/.cache",
    $trivyImage,
    "image", "--quiet", "--scanners", "vuln", "--pkg-types", "os", "--ignore-unfixed",
    "--severity", "HIGH,CRITICAL", "--exit-code", "1", $Image
)
& docker @containerAuditArguments
$containerAuditExitCode = $LASTEXITCODE

$summary = [ordered]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    image = $Image
    policy = [ordered]@{
        python_known_vulnerabilities_allowed = $false
        container_gate_severity = @("HIGH", "CRITICAL")
        container_package_types = @("os")
        container_unfixed_findings_block_release = $false
    }
    tools = [ordered]@{
        pip_audit = $pipAuditVersion
        python_audit_image = $pythonAuditImage
        trivy_image = $trivyImage
    }
    python_vulnerabilities = Get-PythonVulnerabilityCount
    container_vulnerabilities = Get-ContainerVulnerabilityCounts
    python_gate_passed = $pythonAuditExitCode -eq 0
    container_gate_passed = $containerAuditExitCode -eq 0
}
$summary | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $summaryReport -Encoding UTF8
Write-Host "Security reports: $reportPath"

if ($pythonAuditExitCode -ne 0 -or $containerAuditExitCode -ne 0) {
    throw "Security gate failed (pip-audit=$pythonAuditExitCode, trivy=$containerAuditExitCode). Review $summaryReport"
}

Write-Host "Python dependency audit: PASS"
Write-Host "Fixable HIGH/CRITICAL container vulnerability gate: PASS"
