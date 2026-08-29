param(
    [string]$Python = "python",
    [string]$DatabaseUrl = "",
    [string]$ChunkCounts = "10000",
    [ValidateRange(8, 4000)]
    [int]$Dimensions = 2560,
    [ValidateRange(1, 100)]
    [int]$Tenants = 4,
    [ValidateRange(2, 256)]
    [int]$Clusters = 64,
    [ValidateRange(1, 1000)]
    [int]$Queries = 40,
    [ValidateRange(1, 100)]
    [int]$TopK = 10,
    [ValidateRange(1, 200)]
    [int]$AnnOversampling = 20,
    [string]$Concurrency = "1,5,10,20",
    [ValidateRange(2, 100)]
    [int]$HnswM = 32,
    [ValidateRange(4, 1000)]
    [int]$HnswEfConstruction = 128,
    [string]$HnswEfSearchValues = "400,800,1000",
    [ValidateRange(0.0, 1.0)]
    [double]$MinimumNeighborRecall = 0.65,
    [ValidateRange(0.0, 1.0)]
    [double]$MinimumSemanticPrecision = 0.95,
    [ValidateRange(0.0, 1.0)]
    [double]$MinimumSemanticCoverage = 1.0,
    [double]$MaximumP95Ms = 0,
    [switch]$SkipSpeedupGate,
    [switch]$ForceAnnIndex,
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$Root = Split-Path -Parent $PSScriptRoot
if (-not $DatabaseUrl) {
    $DatabaseUrl = $env:JOB_AGENT_DATABASE_URL
}
if (-not $DatabaseUrl) {
    throw "RAG scale benchmark requires -DatabaseUrl or JOB_AGENT_DATABASE_URL."
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $Root "data\eval-reports"
}

$Counts = @(
    $ChunkCounts.Split(",") |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ } |
        ForEach-Object { [int]$_ }
)
if (-not $Counts) {
    throw "ChunkCounts must contain at least one integer."
}
foreach ($Count in $Counts) {
    if ($Count -lt 100 -or $Count -gt 100000) {
        throw "Each chunk count must be between 100 and 100000: $Count"
    }
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$PreviousPythonPath = $env:PYTHONPATH
try {
    $SourcePath = Join-Path $Root "src"
    $env:PYTHONPATH = if ($PreviousPythonPath) {
        "$SourcePath$([IO.Path]::PathSeparator)$PreviousPythonPath"
    }
    else {
        $SourcePath
    }
    foreach ($Count in $Counts) {
        $Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $OutputPath = Join-Path $OutputDirectory "rag-scale-$Count-$Timestamp.json"
        $Arguments = @(
            "-m", "job_hunting_agent.evals.rag_scale_benchmark",
            "--database-url", $DatabaseUrl,
            "--output", $OutputPath,
            "--chunk-count", $Count,
            "--dimensions", $Dimensions,
            "--tenants", $Tenants,
            "--clusters", $Clusters,
            "--queries", $Queries,
            "--top-k", $TopK,
            "--ann-oversampling", $AnnOversampling,
            "--concurrency", $Concurrency,
            "--hnsw-m", $HnswM,
            "--hnsw-ef-construction", $HnswEfConstruction,
            "--hnsw-ef-search-values", $HnswEfSearchValues,
            "--minimum-neighbor-recall", $MinimumNeighborRecall,
            "--minimum-semantic-precision", $MinimumSemanticPrecision,
            "--minimum-semantic-coverage", $MinimumSemanticCoverage
        )
        if ($MaximumP95Ms -gt 0) {
            $Arguments += @("--maximum-p95-ms", $MaximumP95Ms)
        }
        if ($SkipSpeedupGate) {
            $Arguments += "--skip-speedup-gate"
        }
        if ($ForceAnnIndex) {
            $Arguments += "--force-ann-index"
        }
        Write-Host "==> Running isolated pgvector scale benchmark"
        Write-Host "chunks=$Count dimensions=$Dimensions tenants=$Tenants"
        Write-Host "report=$OutputPath"
        & $Python @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "RAG scale benchmark failed. Review $OutputPath."
        }
    }
}
finally {
    $env:PYTHONPATH = $PreviousPythonPath
}

Write-Host "RAG scale benchmark passed."
