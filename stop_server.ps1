# Stop LocalOCR local API server processes inside WSL.
param(
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'

Write-Host "[LocalOCR] Stopping API server on port $Port ..." -ForegroundColor Cyan
$pids = & wsl.exe -d Ubuntu -e /usr/bin/pgrep -f "^/root/localocr-venv/bin/python -m localocr.server"
foreach ($pidText in $pids) {
    $pidText = ($pidText | Out-String).Trim()
    if ($pidText -match '^\d+$') {
        & wsl.exe -d Ubuntu -e /bin/kill -TERM $pidText
    }
}
Start-Sleep -Seconds 2
$remaining = & wsl.exe -d Ubuntu -e /usr/bin/pgrep -f "^/root/localocr-venv/bin/python -m localocr.server"
foreach ($pidText in $remaining) {
    $pidText = ($pidText | Out-String).Trim()
    if ($pidText -match '^\d+$') {
        & wsl.exe -d Ubuntu -e /bin/kill -KILL $pidText
    }
}
Write-Host "[LocalOCR] Stop command sent." -ForegroundColor Green
