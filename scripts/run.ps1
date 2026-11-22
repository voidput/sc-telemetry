# Start the bridge + dashboard (http://127.0.0.1:8765). All arguments pass through, e.g.
#   .\scripts\run.ps1 --record
#   $env:SC_DIR = "D:\Games\Roberts Space Industries\StarCitizen"; .\scripts\run.ps1
# If script execution is blocked: powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1
$ErrorActionPreference = "Stop"
Push-Location (Join-Path $PSScriptRoot "..")
try {
    uv run sctelemetry @args
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code
