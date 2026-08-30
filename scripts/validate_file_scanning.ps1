[CmdletBinding()]
param(
    [string]$Image = "job-hunting-agent:file-scan-acceptance",
    [ValidateRange(1, 168)]
    [int]$MaximumDefinitionAgeHours = 48,
    [ValidateRange(120, 900)]
    [int]$TimeoutSeconds = 420,
    [string]$EvidenceRoot = "data/file-scan-drills",
    [switch]$SkipBuild,
    [switch]$KeepEnvironment
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$Root = Split-Path -Parent $PSScriptRoot
$BaseCompose = Join-Path $Root "compose.yaml"
$AcceptanceCompose = Join-Path $Root "compose.file-scan-test.yaml"
$RunId = "{0}-{1}" -f (Get-Date -Format "yyyyMMdd-HHmmss"), $PID
$ProjectName = "job-agent-file-scan-$($RunId.ToLowerInvariant())"
$ComposeFiles = @("-p", $ProjectName, "-f", $BaseCompose, "-f", $AcceptanceCompose)
$EvidenceBase = if ([IO.Path]::IsPathRooted($EvidenceRoot)) {
    [IO.Path]::GetFullPath($EvidenceRoot)
}
else {
    [IO.Path]::GetFullPath((Join-Path $Root $EvidenceRoot))
}
$ReportDirectory = Join-Path $EvidenceBase $RunId
$ReportPath = Join-Path $ReportDirectory "file-scan-report.json"
$ClamAVImage = "clamav/clamav:1.4.6@sha256:761f6c99b8d9134b39431f8c200189cda749b17310091561bfa8b732f32bfada"

$env:JOB_AGENT_FILE_SCAN_ACCEPTANCE_IMAGE = $Image
$env:JOB_AGENT_CLAMAV_IMAGE = $ClamAVImage
$env:JOB_AGENT_OBJECT_STORAGE_ACCESS_KEY = "filescan$($PID)"
$env:JOB_AGENT_OBJECT_STORAGE_SECRET_KEY = [Guid]::NewGuid().ToString("N")
$env:JOB_AGENT_OBJECT_STORAGE_BUCKET = "file-scan-acceptance"
$env:JOB_AGENT_REDIS_PASSWORD = [Guid]::NewGuid().ToString("N")

$ClamAVVersion = $null
$DefinitionVersion = $null
$DefinitionSignatures = $null
$DefinitionUpdatedAt = $null
$DefinitionAgeHours = $null
$BootstrapResult = $null
$OutageResult = $null
$RecoveryResult = $null
$CleanupResult = $null
$FailureMessage = $null
$TestsPassed = $false

function Invoke-Compose {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & docker compose @ComposeFiles @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose command failed with exit code $LASTEXITCODE."
    }
}

function Invoke-ComposeOutput {
    param([Parameter(Mandatory)][string[]]$Arguments)

    $output = @(& docker compose @ComposeFiles @Arguments)
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose command failed with exit code $LASTEXITCODE."
    }
    return $output
}

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Wait-ServiceHealthy {
    param(
        [Parameter(Mandatory)][string]$Service,
        [Parameter(Mandatory)][int]$Timeout
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($Timeout)
    do {
        $containerId = (
            Invoke-ComposeOutput -Arguments @("ps", "-a", "-q", $Service) |
                Select-Object -First 1
        )
        if ($containerId) {
            $state = (& docker inspect --format "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" $containerId).Trim()
            $stateParts = $state -split "\|", 2
            $containerStatus = $stateParts[0]
            $healthStatus = $stateParts[1]
            if ($containerStatus -eq "running" -and $healthStatus -eq "healthy") {
                return
            }
            if ($containerStatus -in @("exited", "dead")) {
                throw "$Service exited before becoming healthy."
            }
        }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "$Service did not become healthy within $Timeout seconds."
}

function Get-ClamAVDefinitionInfo {
    $lines = @(
        Invoke-ComposeOutput -Arguments @(
            "exec", "-T", "clamav", "sh", "-c",
            'for file in /var/lib/clamav/daily.cld /var/lib/clamav/daily.cvd; do if [ -f "$file" ]; then sigtool --info "$file"; exit 0; fi; done; exit 1'
        )
    )
    $buildLine = $lines | Where-Object { $_ -like "Build time:*" } | Select-Object -First 1
    $versionLine = $lines | Where-Object { $_ -like "Version:*" } | Select-Object -First 1
    $signaturesLine = $lines | Where-Object { $_ -like "Signatures:*" } | Select-Object -First 1
    if (-not $buildLine -or -not $versionLine -or -not $signaturesLine) {
        throw "ClamAV daily database metadata is incomplete."
    }
    $builtAt = [DateTimeOffset]::Parse(
        ($buildLine -replace "^Build time:\s*", ""),
        [Globalization.CultureInfo]::InvariantCulture
    )
    $ageHours = [Math]::Max(
        0,
        [Math]::Round(([DateTimeOffset]::UtcNow - $builtAt).TotalHours, 2)
    )
    return [pscustomobject]@{
        version = ($versionLine -replace "^Version:\s*", "").Trim()
        signatures = [long](($signaturesLine -replace "^Signatures:\s*", "").Trim())
        built_at = $builtAt
        age_hours = $ageHours
    }
}

function Wait-ClamAVDefinitionsFresh {
    param(
        [Parameter(Mandatory)][int]$MaximumAgeHours,
        [Parameter(Mandatory)][int]$Timeout
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($Timeout)
    do {
        $definition = Get-ClamAVDefinitionInfo
        if ($definition.age_hours -le $MaximumAgeHours) {
            return $definition
        }
        Start-Sleep -Seconds 5
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "ClamAV definitions are $($definition.age_hours) hours old; maximum allowed is $MaximumAgeHours."
}

function Invoke-WebPython {
    param(
        [Parameter(Mandatory)][string]$Code,
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
    $output = Invoke-ComposeOutput -Arguments $arguments
    return (($output -join "`n") | ConvertFrom-Json)
}

$BootstrapCode = @'
import io
import json
import os

from docx import Document

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.file_scanning import FileInfectedError
from job_hunting_agent.models import CandidateProfileInput


def build_docx(*paragraphs: str) -> bytes:
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


backend = JobHuntingApp(env_path="/app/.env")
try:
    backend.initialize()
    assert backend.file_scanning_settings.backend == "clamav"
    assert backend.file_scanner.engine == "clamav"
    account = backend.store.create_account(
        os.environ["JOB_AGENT_ACCEPTANCE_EMAIL"],
        "file-scan-acceptance-password-hash",
        display_name="File scan acceptance",
    )
    candidate_id = backend.save_candidate_profile(
        CandidateProfileInput(
            name="File Scan Candidate",
            status="available",
            education="bachelor",
            experience_years=2,
            skills={"Python": "project"},
            preferred_cities=["Hangzhou"],
            salary_floor_k=10,
            expected_salary_k=15,
            target_directions=["backend"],
            unacceptable=[],
        ),
        account_id=account.id,
    )

    clean_content = build_docx("ClamAV acceptance clean resume", "Python FastAPI")
    clean = backend.upload_resume_document(
        candidate_id,
        "clean-resume.docx",
        clean_content,
        account_id=account.id,
    )
    assert clean.status == "ready"
    assert clean.scan_status == "clean"
    assert clean.scan_engine == "clamav"
    assert clean.long_text_id is not None
    assert backend.read_resume_file(clean) == clean_content

    eicar_content = (
        b"X5O!P%@AP[4" + bytes([92]) +
        b"PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    )
    infected_rejected = False
    try:
        backend.upload_resume_document(
            candidate_id,
            "eicar-resume.docx",
            eicar_content,
            account_id=account.id,
        )
    except FileInfectedError:
        infected_rejected = True
    assert infected_rejected

    infected = max(
        (
            item
            for item in backend.list_resume_artifacts(candidate_id, account_id=account.id)
            if item.original_filename == "eicar-resume.docx"
        ),
        key=lambda item: item.id,
    )
    assert infected.status == "quarantined"
    assert infected.scan_status == "infected"
    assert infected.scan_engine == "clamav"
    assert infected.long_text_id is None
    assert backend.resume_files.read(infected.storage_key) == eicar_content
    try:
        backend.read_resume_file(infected)
    except KeyError:
        pass
    else:
        raise AssertionError("Quarantined content remained available through the download boundary.")

    print(json.dumps({
        "account_id": account.id,
        "candidate_id": candidate_id,
        "clean_artifact_id": clean.id,
        "infected_artifact_id": infected.id,
        "clean_scan_passed": True,
        "eicar_rejected": infected_rejected,
        "infected_object_retained_for_controlled_cleanup": True,
    }))
finally:
    backend.store.close()
'@

$OutageCode = @'
import io
import json
import os

from docx import Document

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.file_scanning import FileScannerUnavailableError


document = Document()
document.add_paragraph("Scanner outage acceptance content")
output = io.BytesIO()
document.save(output)
content = output.getvalue()

backend = JobHuntingApp(env_path="/app/.env")
try:
    account_id = int(os.environ["JOB_AGENT_ACCEPTANCE_ACCOUNT_ID"])
    candidate_id = int(os.environ["JOB_AGENT_ACCEPTANCE_CANDIDATE_ID"])
    unavailable_rejected = False
    try:
        backend.upload_resume_document(
            candidate_id,
            "scanner-unavailable.docx",
            content,
            account_id=account_id,
        )
    except FileScannerUnavailableError:
        unavailable_rejected = True
    assert unavailable_rejected

    failed = max(
        (
            item
            for item in backend.list_resume_artifacts(candidate_id, account_id=account_id)
            if item.original_filename == "scanner-unavailable.docx"
        ),
        key=lambda item: item.id,
    )
    assert failed.status == "quarantined"
    assert failed.scan_status == "error"
    assert failed.scan_engine == "clamav"
    assert failed.long_text_id is None
    assert backend.resume_files.read(failed.storage_key) == content
    try:
        backend.read_resume_file(failed)
    except KeyError:
        pass
    else:
        raise AssertionError("Scan-error content remained available through the download boundary.")

    print(json.dumps({
        "unavailable_artifact_id": failed.id,
        "scanner_unavailable_rejected": unavailable_rejected,
        "error_object_retained_for_controlled_cleanup": True,
    }))
finally:
    backend.store.close()
'@

$RecoveryCode = @'
import io
import json
import os

from docx import Document

from job_hunting_agent.app import JobHuntingApp


document = Document()
document.add_paragraph("ClamAV service recovery acceptance content")
output = io.BytesIO()
document.save(output)
content = output.getvalue()

backend = JobHuntingApp(env_path="/app/.env")
try:
    account_id = int(os.environ["JOB_AGENT_ACCEPTANCE_ACCOUNT_ID"])
    candidate_id = int(os.environ["JOB_AGENT_ACCEPTANCE_CANDIDATE_ID"])
    recovered = backend.upload_resume_document(
        candidate_id,
        "scanner-recovered.docx",
        content,
        account_id=account_id,
    )
    assert recovered.status == "ready"
    assert recovered.scan_status == "clean"
    assert recovered.scan_engine == "clamav"
    assert recovered.long_text_id is not None
    assert backend.read_resume_file(recovered) == content
    print(json.dumps({
        "recovered_artifact_id": recovered.id,
        "scan_after_restart_passed": True,
    }))
finally:
    backend.store.close()
'@

$CleanupCode = @'
import json
import os

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.object_storage import ObjectNotFoundError


backend = JobHuntingApp(env_path="/app/.env")
try:
    account_id = int(os.environ["JOB_AGENT_ACCEPTANCE_ACCOUNT_ID"])
    artifact_ids = [
        int(value)
        for value in os.environ["JOB_AGENT_ACCEPTANCE_ARTIFACT_IDS"].split(",")
        if value
    ]
    storage_keys = []
    for artifact_id in artifact_ids:
        artifact = backend.get_resume_artifact(artifact_id, account_id=account_id)
        storage_keys.append(artifact.storage_key)
        backend.delete_resume_artifact(artifact_id, account_id=account_id)
        try:
            backend.get_resume_artifact(artifact_id, account_id=account_id)
        except KeyError:
            pass
        else:
            raise AssertionError(f"Artifact {artifact_id} still exists after deletion.")

    for storage_key in storage_keys:
        try:
            backend.resume_files.read(storage_key)
        except ObjectNotFoundError:
            pass
        else:
            raise AssertionError(f"Object {storage_key} still exists after deletion.")

    object_response = backend.resume_files.client.list_objects_v2(
        Bucket=backend.resume_files.bucket,
        Prefix=f"account-{account_id}/",
    )
    remaining_objects = int(object_response.get("KeyCount", 0))
    assert remaining_objects == 0

    with backend.store.connect() as conn:
        remaining_artifacts = int(conn.execute(
            "SELECT COUNT(*) AS count FROM resume_artifacts WHERE account_id = ?",
            (account_id,),
        ).fetchone()["count"])
        assert remaining_artifacts == 0
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        remaining_accounts = int(conn.execute(
            "SELECT COUNT(*) AS count FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()["count"])
        remaining_long_texts = int(conn.execute(
            "SELECT COUNT(*) AS count FROM long_texts WHERE account_id = ?",
            (account_id,),
        ).fetchone()["count"])
    assert remaining_accounts == 0
    assert remaining_long_texts == 0

    print(json.dumps({
        "deleted_artifacts": len(artifact_ids),
        "remaining_artifacts": remaining_artifacts,
        "remaining_objects": remaining_objects,
        "remaining_accounts": remaining_accounts,
        "remaining_long_texts": remaining_long_texts,
        "database_and_object_cleanup_passed": True,
    }))
finally:
    backend.store.close()
'@

New-Item -ItemType Directory -Force -Path $ReportDirectory | Out-Null

try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "docker command was not found. Start Docker Desktop and try again."
    }
    if ([string]::IsNullOrWhiteSpace($Image)) {
        throw "Image must not be empty."
    }

    Write-Host "==> Checking Docker and isolated Compose configuration"
    Invoke-Docker -Arguments @("version")
    Invoke-Compose -Arguments @("config", "--quiet")

    if (-not $SkipBuild) {
        Write-Host "==> Building the current application image"
        Invoke-Docker -Arguments @("build", "--pull", "--tag", $Image, $Root)
    }
    else {
        Write-Host "==> Reusing application image: $Image"
        $null = & docker image inspect $Image
        if ($LASTEXITCODE -ne 0) {
            throw "The requested image does not exist: $Image"
        }
    }

    Write-Host "==> Starting isolated PostgreSQL, Redis, MinIO and ClamAV"
    Invoke-Compose -Arguments @("up", "-d", "--no-build", "postgres", "redis", "minio", "clamav")
    Wait-ServiceHealthy -Service "clamav" -Timeout $TimeoutSeconds

    $definition = Wait-ClamAVDefinitionsFresh `
        -MaximumAgeHours $MaximumDefinitionAgeHours `
        -Timeout $TimeoutSeconds
    $DefinitionVersion = $definition.version
    $DefinitionSignatures = $definition.signatures
    $DefinitionUpdatedAt = $definition.built_at.UtcDateTime.ToString("o")
    $DefinitionAgeHours = $definition.age_hours
    Invoke-Compose -Arguments @("exec", "-T", "clamav", "clamdscan", "--reload")
    Start-Sleep -Seconds 2
    $ClamAVVersion = ((
        Invoke-ComposeOutput -Arguments @("exec", "-T", "clamav", "clamdscan", "--version") |
            Select-Object -Last 1
    ).Trim() -split "/")[0].Trim()
    Write-Host "ClamAV signature freshness: PASS ($DefinitionAgeHours hours old)"

    Write-Host "==> Applying migrations and starting the production-configured Web service"
    Invoke-Compose -Arguments @("up", "-d", "--no-build", "web")
    Wait-ServiceHealthy -Service "web" -Timeout $TimeoutSeconds

    $acceptanceEmail = "file-scan-$RunId@example.invalid"
    Write-Host "==> Checking clean-file and EICAR behavior through the application boundary"
    $BootstrapResult = Invoke-WebPython -Code $BootstrapCode -Environment @{
        JOB_AGENT_ACCEPTANCE_EMAIL = $acceptanceEmail
    }
    Write-Host "Clean resume scan and EICAR quarantine: PASS"

    Write-Host "==> Stopping ClamAV and checking fail-closed behavior"
    Invoke-Compose -Arguments @("stop", "clamav")
    $OutageResult = Invoke-WebPython -Code $OutageCode -Environment @{
        JOB_AGENT_ACCEPTANCE_ACCOUNT_ID = [string]$BootstrapResult.account_id
        JOB_AGENT_ACCEPTANCE_CANDIDATE_ID = [string]$BootstrapResult.candidate_id
    }
    Write-Host "Scanner outage quarantine: PASS"

    Write-Host "==> Restarting ClamAV and checking recovery without restarting Web"
    Invoke-Compose -Arguments @("start", "clamav")
    Wait-ServiceHealthy -Service "clamav" -Timeout $TimeoutSeconds
    $RecoveryResult = Invoke-WebPython -Code $RecoveryCode -Environment @{
        JOB_AGENT_ACCEPTANCE_ACCOUNT_ID = [string]$BootstrapResult.account_id
        JOB_AGENT_ACCEPTANCE_CANDIDATE_ID = [string]$BootstrapResult.candidate_id
    }
    Write-Host "Scan after ClamAV restart: PASS"

    Write-Host "==> Deleting clean and quarantined records and their MinIO objects"
    $artifactIds = @(
        $BootstrapResult.clean_artifact_id,
        $BootstrapResult.infected_artifact_id,
        $OutageResult.unavailable_artifact_id,
        $RecoveryResult.recovered_artifact_id
    ) -join ","
    $CleanupResult = Invoke-WebPython -Code $CleanupCode -Environment @{
        JOB_AGENT_ACCEPTANCE_ACCOUNT_ID = [string]$BootstrapResult.account_id
        JOB_AGENT_ACCEPTANCE_ARTIFACT_IDS = $artifactIds
    }
    Write-Host "Database and object-storage quarantine cleanup: PASS"
    $TestsPassed = $true
}
catch {
    $FailureMessage = $_.Exception.Message
    throw
}
finally {
    $report = [ordered]@{
        generated_at = [DateTime]::UtcNow.ToString("o")
        run_id = $RunId
        project_name = $ProjectName
        application_image = $Image
        clamav_image = $ClamAVImage
        clamav_version = $ClamAVVersion
        definition_version = $DefinitionVersion
        definition_signatures = $DefinitionSignatures
        definition_updated_at = $DefinitionUpdatedAt
        definition_age_hours = $DefinitionAgeHours
        maximum_definition_age_hours = $MaximumDefinitionAgeHours
        clean_and_eicar = $BootstrapResult
        scanner_outage = $OutageResult
        scanner_recovery = $RecoveryResult
        cleanup = $CleanupResult
        tests_passed = $TestsPassed
        success = $TestsPassed -and [string]::IsNullOrWhiteSpace($FailureMessage)
        error = $FailureMessage
    }
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    Write-Host "Acceptance report: $ReportPath"

    if ($KeepEnvironment) {
        Write-Warning "Keeping isolated Compose project for inspection: $ProjectName"
    }
    else {
        & docker compose @ComposeFiles down -v --remove-orphans
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Automatic cleanup failed. Remove the isolated project manually: docker compose -p $ProjectName down -v"
        }
    }
}
