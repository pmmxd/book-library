$ErrorActionPreference = "Stop"
$app = Split-Path -Parent $MyInvocation.MyCommand.Path
$project = Split-Path -Parent $app
$root = Split-Path -Parent $project
$data = Join-Path $project "data"
$pidFile = Join-Path $data "runtime\bridge.pid"
$statusUrl = "http://127.0.0.1:18767/api/status"

if (Test-Path -LiteralPath $pidFile) {
    try {
        $oldPid = [int](Get-Content -LiteralPath $pidFile -Raw).Trim()
        if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) {
            $raw = & curl.exe --noproxy "*" --silent --max-time 2 $statusUrl
            if ($LASTEXITCODE -eq 0 -and $raw) {
                $status = $raw | ConvertFrom-Json
                if ($status.service -eq "book-bridge") {
                    Write-Host "Book bridge is already running. PID=$oldPid"
                    exit 0
                }
            }
            Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
        }
    } catch {
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

$pythonw = Get-Command pythonw.exe -ErrorAction Stop
Start-Process `
    -FilePath $pythonw.Source `
    -ArgumentList @(
        "`"$app\bridge.py`"",
        "--root", "`"$root`"",
        "--output", "`"$data`"",
        "--port", "18767"
    ) `
    -WorkingDirectory $app `
    -WindowStyle Hidden

for ($i = 0; $i -lt 40; $i++) {
    $raw = & curl.exe --noproxy "*" --silent --max-time 2 $statusUrl
    if ($LASTEXITCODE -eq 0 -and $raw) {
        $status = $raw | ConvertFrom-Json
        if ($status.service -eq "book-bridge") {
            Write-Host "Book bridge started at http://127.0.0.1:18767/"
            exit 0
        }
    }
    Start-Sleep -Milliseconds 500
}
throw "Book bridge failed to start."
