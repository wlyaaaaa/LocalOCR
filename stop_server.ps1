# Stop LocalOCR local API server processes inside WSL.
param(
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'

Write-Host "[LocalOCR] Stopping API server on port $Port ..." -ForegroundColor Cyan
$cmd = "pkill -f 'localocr.server' || true"
wsl -d Ubuntu -e bash -lc $cmd
Write-Host "[LocalOCR] Stop command sent." -ForegroundColor Green
