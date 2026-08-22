param(
    [string]$Python = "python",
    [string]$RagCases = "",
    [string]$ConversationCases = "",
    [int]$AccountId = 0,
    [int]$TopK = 5
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

function Invoke-Check {
    param(
        [string]$Name,
        [scriptblock]$Command
    )

    Write-Host "==> $Name"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE."
    }
}

Invoke-Check "Python tests" {
    & $Python -m pytest -q
}

Invoke-Check "Ruff" {
    & ruff check src tests alembic
}

Invoke-Check "Compile Python sources" {
    & $Python -m compileall -q src tests alembic
}

Invoke-Check "Frontend regressions" {
    Get-ChildItem tests -Filter "frontend_*.mjs" |
        Sort-Object Name |
        ForEach-Object {
            & node $_.FullName
            if ($LASTEXITCODE -ne 0) {
                throw "Frontend regression failed: $($_.Name)"
            }
        }
}

if ($RagCases) {
    Invoke-Check "RAG retrieval eval" {
        $args = @(
            "-m",
            "job_hunting_agent.evals.rag_eval",
            "--cases",
            $RagCases,
            "--top-k",
            [string]$TopK
        )
        if ($AccountId -gt 0) {
            $args += @("--account-id", [string]$AccountId)
        }
        & $Python @args
    }
}

if ($ConversationCases) {
    Invoke-Check "Conversation profile eval" {
        $args = @(
            "-m",
            "job_hunting_agent.evals.conversation_eval",
            "--cases",
            $ConversationCases
        )
        & $Python @args
    }
}

Write-Host "Enterprise acceptance checks passed."
