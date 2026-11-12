$ErrorActionPreference = "Stop"

$app = Split-Path -Parent $MyInvocation.MyCommand.Path
$project = Split-Path -Parent $app
$runtime = Join-Path $project "data\runtime"
$pidFile = Join-Path $runtime "server.pid"
$portFile = Join-Path $runtime "server.port"

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host "Book library server is not running."
    exit 0
}

try {
    $serverPid = [int](Get-Content -LiteralPath $pidFile -Raw).Trim()
    Stop-Process -Id $serverPid -Force -ErrorAction SilentlyContinue
    Write-Host "Book library server stopped. PID=$serverPid"
} finally {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $portFile -Force -ErrorAction SilentlyContinue
}
