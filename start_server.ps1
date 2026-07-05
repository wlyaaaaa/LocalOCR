# LocalOCR local API server launcher.
param(
    [int]$Port = 8765,
    [string]$HostAddress = "127.0.0.1",
    [int]$StartupTimeoutSec = 600
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerDir = Join-Path $ScriptDir "_server"
New-Item -ItemType Directory -Force -Path $ServerDir | Out-Null
$LogPath = Join-Path $ServerDir "localocr-api.log"
$PidPath = Join-Path $ServerDir "wsl-server.pid"

function Get-LocalOcrHealth {
    try {
        $health = Invoke-RestMethod -Uri "http://${HostAddress}:$Port/health" -Method Get -TimeoutSec 15
        if ($health.ok) {
            return $health
        }
    } catch {
        return $null
    }
    return $null
}

$mutexName = "Local\LocalOCR-API-$Port"
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$hasMutex = $false

try {
    $existing = Get-LocalOcrHealth
    if ($existing) {
        Write-Host "[LocalOCR] API already running. GPU: $($existing.gpu)" -ForegroundColor Green
        return
    }

    $hasMutex = $mutex.WaitOne([TimeSpan]::FromSeconds($StartupTimeoutSec))
    if (-not $hasMutex) {
        throw "Timed out waiting for LocalOCR startup lock after ${StartupTimeoutSec}s: $mutexName"
    }

    $existing = Get-LocalOcrHealth
    if ($existing) {
        Write-Host "[LocalOCR] API already running. GPU: $($existing.gpu)" -ForegroundColor Green
        return
    }

    Write-Host "[LocalOCR] Starting API server at http://${HostAddress}:$Port ..." -ForegroundColor Cyan
    Write-Host "[LocalOCR] Log: $LogPath" -ForegroundColor DarkGray
    $wslLogPath = "/mnt/e/LocalOCR/_server/localocr-api.log"
    $wslCommand = "cd /mnt/e/LocalOCR && exec scripts/run_in_wsl.sh -m localocr.server --host '$HostAddress' --port $Port >> '$wslLogPath' 2>&1"
    $escapedCommand = $wslCommand.Replace('"', '\"')
    $arguments = "-d Ubuntu -e bash -lc `"$escapedCommand`""
    $process = Start-Process -FilePath "wsl.exe" -ArgumentList $arguments -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath $PidPath -Value $process.Id -Encoding ascii

    $deadline = (Get-Date).AddSeconds($StartupTimeoutSec)
    $lastError = $null
    do {
        Start-Sleep -Seconds 2
        try {
            $health = Invoke-RestMethod -Uri "http://${HostAddress}:$Port/health" -Method Get -TimeoutSec 15
            if ($health.ok) {
                Write-Host "[LocalOCR] API ready. GPU: $($health.gpu)" -ForegroundColor Green
                return
            }
        } catch {
            $lastError = $_
        }
    } while ((Get-Date) -lt $deadline)

    Write-Host "[LocalOCR] Server did not become ready before ${StartupTimeoutSec}s timeout." -ForegroundColor Yellow
    if (Test-Path -LiteralPath $LogPath) {
        Write-Host "[LocalOCR] Last log lines:" -ForegroundColor Yellow
        Get-Content -LiteralPath $LogPath -Tail 40
    }
    if ($lastError) {
        Write-Host "[LocalOCR] Last health-check error: $($lastError.Exception.Message)" -ForegroundColor Yellow
    }
    throw "LocalOCR API server startup timed out."
} finally {
    if ($hasMutex) {
        $mutex.ReleaseMutex() | Out-Null
    }
    $mutex.Dispose()
}
