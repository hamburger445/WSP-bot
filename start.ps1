$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .\.venv\Scripts\python.exe)) {
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}
if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
  Write-Host "Created .env — add DISCORD_TOKEN and related values, then re-run."
  exit 1
}
.\.venv\Scripts\python.exe main.py
