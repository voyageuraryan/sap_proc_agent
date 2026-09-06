# One-command setup on Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#
# Installs uv if it is missing, builds the environment from the lockfile,
# creates .env, and proves the whole thing works by running the test suite.

$ErrorActionPreference = "Stop"

function Step($message) { Write-Host "`n==> $message" -ForegroundColor Cyan }

Step "Checking for uv"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found, installing..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
uv --version

Step "Building the environment from uv.lock"
# --locked, not --upgrade: the lockfile is the reproducibility guarantee, and a
# setup script that quietly resolves new versions defeats it.
uv sync --locked

Step "Configuring .env"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env -- add your provider key before running the agent." -ForegroundColor Yellow
} else {
    Write-Host ".env already exists, leaving it alone."
}

Step "Verifying the dataset is reproducible"
uv run python -m generator.cli

Step "Running the test suite"
uv run pytest -q

Write-Host "`nReady." -ForegroundColor Green
Write-Host @"

  Start the services (two terminals):
    uv run uvicorn mock_erp.app:app --reload --port 8000
    uv run uvicorn review_ui.app:app --reload --port 8001

  Then:
    uv run python scripts/demo.py                       the walkthrough
    uv run proc-evals --split all --mode baseline       score 200 scenarios
    start http://localhost:8001                         the review queue
"@
