# Stop LocalOCR local API server processes inside WSL.
param(
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'

Write-Host "[LocalOCR] Stopping API server on port $Port ..." -ForegroundColor Cyan
$pids = & wsl.exe -d Ubuntu -e bash -lc "pgrep -f '^/root/localocr-venv/bin/python -m localocr.server' || true"
foreach ($pidText in $pids) {
    $pidText = ($pidText | Out-String).Trim()
    if ($pidText -match '^\d+$') {
        & wsl.exe -d Ubuntu -e bash -lc "kill $pidText || true"
    }
}
Write-Host "[LocalOCR] Stop command sent." -ForegroundColor Green
