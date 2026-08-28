param(
    [string]$Python = "python",
    [string]$Suite = "",
    [string]$EnvFile = "",
    [string]$DatabaseUrl = "",
    [ValidateSet("configured", "local_hash")]
    [string]$EmbeddingMode = "configured",
    [switch]$NoRerank,
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$Root = Split-Path -Parent $PSScriptRoot
if (-not $Suite) {
    $Suite = Join-Path $Root "evals\rag\golden_suite.json"
}
if (-not $EnvFile) {
    $EnvFile = Join-Path $Root ".env"
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $Root "data\eval-reports"
}

if (-not (Test-Path -LiteralPath $Suite -PathType Leaf)) {
    throw "RAG benchmark suite not found: $Suite"
}
if ($EmbeddingMode -eq "configured" -and -not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Configured embedding mode requires an env file: $EnvFile"
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$OutputPath = Join-Path $OutputDirectory "rag-benchmark-$Timestamp.json"
$Arguments = @(
    "-m",
    "job_hunting_agent.evals.rag_benchmark",
    "--suite",
    $Suite,
    "--env-file",
    $EnvFile,
    "--embedding-mode",
    $EmbeddingMode,
    "--output",
    $OutputPath
)
if ($DatabaseUrl) {
    $Arguments += @("--database-url", $DatabaseUrl)
}
if ($NoRerank) {
    $Arguments += "--no-rerank"
}

$PreviousPythonPath = $env:PYTHONPATH
try {
    $SourcePath = Join-Path $Root "src"
    $env:PYTHONPATH = if ($PreviousPythonPath) {
        "$SourcePath$([IO.Path]::PathSeparator)$PreviousPythonPath"
    }
    else {
        $SourcePath
    }
    Write-Host "==> Running isolated RAG retrieval benchmark"
    Write-Host "suite=$Suite"
    Write-Host "embedding_mode=$EmbeddingMode"
    Write-Host "report=$OutputPath"
    & $Python @Arguments
    $ExitCode = $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $PreviousPythonPath
}

if ($ExitCode -ne 0) {
    throw "RAG retrieval quality gate failed with exit code $ExitCode. Review $OutputPath."
}
Write-Host "RAG retrieval quality gate passed."
