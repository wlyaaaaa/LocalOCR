# LocalOCR local API server launcher.
param(
    [int]$Port = 8765,
    [string]$HostAddress = "127.0.0.1"
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerDir = Join-Path $ScriptDir "_server"
New-Item -ItemType Directory -Force -Path $ServerDir | Out-Null
$LogPath = Join-Path $ServerDir "localocr-api.log"

try {
    $existing = Invoke-RestMethod -Uri "http://${HostAddress}:$Port/health" -Method Get -TimeoutSec 15
    if ($existing.ok) {
        Write-Host "[LocalOCR] API already running. GPU: $($existing.gpu)" -ForegroundColor Green
        exit 0
    }
} catch {
    # Not running yet.
}

Write-Host "[LocalOCR] Starting API server at http://${HostAddress}:$Port ..." -ForegroundColor Cyan
Write-Host "[LocalOCR] Log: $LogPath" -ForegroundColor DarkGray
wsl -d Ubuntu -e bash /mnt/e/LocalOCR/scripts/start_server_background.sh $HostAddress $Port | Out-Null

$deadline = (Get-Date).AddSeconds(180)
$lastError = $null
do {
    Start-Sleep -Seconds 2
    try {
        $health = Invoke-RestMethod -Uri "http://${HostAddress}:$Port/health" -Method Get -TimeoutSec 15
        if ($health.ok) {
            Write-Host "[LocalOCR] API ready. GPU: $($health.gpu)" -ForegroundColor Green
            exit 0
        }
    } catch {
        $lastError = $_
    }
} while ((Get-Date) -lt $deadline)

Write-Host "[LocalOCR] Server did not become ready before timeout." -ForegroundColor Yellow
if (Test-Path -LiteralPath $LogPath) {
    Write-Host "[LocalOCR] Last log lines:" -ForegroundColor Yellow
    Get-Content -LiteralPath $LogPath -Tail 40
}
if ($lastError) {
    Write-Host "[LocalOCR] Last health-check error: $($lastError.Exception.Message)" -ForegroundColor Yellow
}
throw "LocalOCR API server startup timed out."
