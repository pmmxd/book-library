$ErrorActionPreference = "Stop"
$app = Split-Path -Parent $MyInvocation.MyCommand.Path
$project = Split-Path -Parent $app
$pidFile = Join-Path $project "data\runtime\bridge.pid"

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host "Book bridge is not running."
    exit 0
}
try {
    $bridgePid = [int](Get-Content -LiteralPath $pidFile -Raw).Trim()
    Stop-Process -Id $bridgePid -Force -ErrorAction SilentlyContinue
    Write-Host "Book bridge stopped. PID=$bridgePid"
} finally {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}
