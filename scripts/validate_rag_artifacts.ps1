param(
    [string]$Python = "python",
    [string]$Suite = "",
    [ValidateSet("smoke", "release")]
    [string]$BenchmarkRole = "smoke",
    [string]$EnvFile = "",
    [string]$DatabaseUrl = "",
    [ValidateSet("configured", "local_hash")]
    [string]$EmbeddingMode = "configured",
    [ValidateSet("configured", "disabled")]
    [string]$VisualMode = "configured",
    [switch]$NoRerank,
    [switch]$TuneParameters,
    [string]$TuneKValues = "10,15,20,30,40",
    [string]$TuneNValues = "3,5",
    [ValidateRange(1, 5)]
    [int]$TuningRepetitions = 1,
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$Root = Split-Path -Parent $PSScriptRoot
if (-not $Suite) {
    $SuiteName = if ($BenchmarkRole -eq "release") {
        "github_hard_negative_suite.json"
    }
    else {
        "github_artifact_suite.json"
    }
    $Suite = Join-Path $Root "evals\rag\$SuiteName"
}
if (-not $EnvFile) {
    $EnvFile = Join-Path $Root ".env"
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $Root "data\eval-reports"
}

if (-not (Test-Path -LiteralPath $Suite -PathType Leaf)) {
    throw "GitHub artifact benchmark suite not found: $Suite"
}
if (($EmbeddingMode -eq "configured" -or $VisualMode -eq "configured") -and
    -not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Configured model mode requires an env file: $EnvFile"
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$OutputPath = Join-Path $OutputDirectory "rag-artifacts-$BenchmarkRole-$Timestamp.json"
$Arguments = @(
    "-m",
    "job_hunting_agent.evals.rag_artifact_benchmark",
    "--suite",
    $Suite,
    "--env-file",
    $EnvFile,
    "--embedding-mode",
    $EmbeddingMode,
    "--visual-mode",
    $VisualMode,
    "--output",
    $OutputPath
)
if ($DatabaseUrl) {
    $Arguments += @("--database-url", $DatabaseUrl)
}
if ($NoRerank) {
    $Arguments += "--no-rerank"
}
if ($TuneParameters) {
    $Arguments += @(
        "--tune-parameters",
        "--tune-k-values",
        $TuneKValues,
        "--tune-n-values",
        $TuneNValues,
        "--tuning-repetitions",
        $TuningRepetitions
    )
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
    Write-Host "==> Running pinned GitHub artifact RAG benchmark"
    Write-Host "suite=$Suite"
    Write-Host "benchmark_role=$BenchmarkRole"
    Write-Host "embedding_mode=$EmbeddingMode"
    Write-Host "visual_mode=$VisualMode"
    Write-Host "tune_parameters=$TuneParameters"
    Write-Host "tuning_repetitions=$TuningRepetitions"
    Write-Host "report=$OutputPath"
    & $Python @Arguments
    $ExitCode = $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $PreviousPythonPath
}

if ($ExitCode -ne 0) {
    throw "GitHub artifact RAG quality gate failed with exit code $ExitCode. Review $OutputPath."
}
Write-Host "GitHub artifact RAG quality gate passed."
