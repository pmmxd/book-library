$ErrorActionPreference = "Stop"

$app = Split-Path -Parent $MyInvocation.MyCommand.Path
$project = Split-Path -Parent $app
$root = Split-Path -Parent $project
$data = Join-Path $project "data"
$pidFile = Join-Path $data "runtime\server.pid"
$portFile = Join-Path $data "runtime\server.port"
$server = Join-Path $app "server.py"

if (Test-Path -LiteralPath $pidFile) {
    try {
        $oldPid = [int](Get-Content -LiteralPath $pidFile -Raw).Trim()
        if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) {
            if (Test-Path -LiteralPath $portFile) {
                $oldPort = [int](Get-Content -LiteralPath $portFile -Raw).Trim()
                try {
                    $raw = & curl.exe --noproxy "*" --silent --max-time 2 "http://127.0.0.1:$oldPort/api/status"
                    if ($LASTEXITCODE -eq 0 -and $raw) {
                        $status = $raw | ConvertFrom-Json
                        if ($status.ok -and $status.service -eq "book-library" -and $status.root -eq $root) {
                            Write-Host "Book library server is already running. PID=$oldPid"
                            Start-Process "http://127.0.0.1:$oldPort/"
                            exit 0
                        }
                    }
                } catch {
                }
            }
            Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
        }
    } catch {
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $portFile -Force -ErrorAction SilentlyContinue
}

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = $listener.LocalEndpoint.Port
$listener.Stop()
$statusUrl = "http://127.0.0.1:$port/api/status"
$siteUrl = "http://127.0.0.1:$port/"

$pythonw = Get-Command pythonw.exe -ErrorAction Stop
$arguments = @(
    "`"$server`"",
    "--root", "`"$root`"",
    "--output", "`"$data`"",
    "--host", "127.0.0.1",
    "--port", "$port"
)

Start-Process `
    -FilePath $pythonw.Source `
    -ArgumentList $arguments `
    -WorkingDirectory $app `
    -WindowStyle Hidden

$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $raw = & curl.exe --noproxy "*" --silent --max-time 2 $statusUrl
        if ($LASTEXITCODE -eq 0 -and $raw) {
            $status = $raw | ConvertFrom-Json
            if ($status.ok -and $status.service -eq "book-library" -and $status.root -eq $root) {
                $ready = $true
                break
            }
        }
    } catch {
    }
    Start-Sleep -Milliseconds 500
}

if (-not $ready) {
    throw "Book library server failed to start. Check server.log."
}

Set-Content -LiteralPath $portFile -Value $port -Encoding Ascii
Write-Host "Book library server started at $siteUrl"
Start-Process $siteUrl
