param(
    [string]$Python = "E:\Anaconda\envs\langchain1.2\python.exe",
    [string]$RagCases = "",
    [string]$ConversationCases = "",
    [int]$AccountId = 0,
    [int]$TopK = 5
)

$ErrorActionPreference = "Stop"

function Invoke-Check {
    param(
        [string]$Name,
        [scriptblock]$Command
    )

    Write-Host "==> $Name"
    & $Command
}

Invoke-Check "Python tests" {
    & $Python -m pytest -q
}

Invoke-Check "Ruff" {
    & ruff check src tests
}

Invoke-Check "Compile Python sources" {
    & $Python -m compileall -q src tests
}

Invoke-Check "Admin frontend regression" {
    & node tests/frontend_admin_usage_regression.mjs
}

Invoke-Check "Project review frontend regression" {
    & node tests/frontend_project_review_regression.mjs
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
