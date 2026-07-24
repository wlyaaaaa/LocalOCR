# LocalOCR local API server launcher.
param(
    [int]$Port = 18665,
    [string]$HostAddress = "127.0.0.1",
    [int]$StartupTimeoutSec = 600
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerDir = Join-Path $ScriptDir "_server"
New-Item -ItemType Directory -Force -Path $ServerDir | Out-Null
$LogPath = Join-Path $ServerDir "localocr-api.log"
$PidPath = Join-Path $ServerDir "wsl-server.pid"

function Assert-LocalOcrHealthPayload {
    param(
        [AllowNull()]$Health
    )

    if ($null -eq $Health) {
        return $false
    }
    if ($Health.PSObject.Properties.Name -contains "ok" -and $Health.ok) {
        return $true
    }

    $summary = ""
    try {
        $summary = ($Health | ConvertTo-Json -Depth 6 -Compress)
    } catch {
        $summary = [string]$Health
    }
    throw "[LocalOCR] Port $Port responded to /health, but it is a non-LocalOCR service. Use -Port to choose another port. Response: $summary"
}

function Get-LocalOcrHealth {
    try {
        $health = Invoke-RestMethod -Uri "http://${HostAddress}:$Port/health" -Method Get -TimeoutSec 15
        if (Assert-LocalOcrHealthPayload -Health $health) {
            return $health
        }
    } catch {
        if ($_.Exception.Message -like "*non-LocalOCR service*") {
            throw
        }
        return $null
    }
    return $null
}

function Get-LocalOcrProcess {
    if (-not (Test-Path -LiteralPath $PidPath)) {
        return $null
    }
    try {
        $rawPid = Get-Content -LiteralPath $PidPath -ErrorAction Stop | Select-Object -First 1
        if (-not $rawPid) {
            return $null
        }
        $serverPid = 0
        if (-not [int]::TryParse($rawPid.ToString().Trim(), [ref]$serverPid)) {
            return $null
        }
        $process = Get-Process -Id $serverPid -ErrorAction Stop
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid" -ErrorAction Stop
        if ($processInfo.CommandLine -notlike "*localocr.server*") {
            return $null
        }
        return $process
    } catch {
        return $null
    }
}

function Assert-LocalOcrPortBindable {
    $listener = $null
    try {
        $ipAddress = [System.Net.IPAddress]::Parse($HostAddress)
        $listener = [System.Net.Sockets.TcpListener]::new($ipAddress, $Port)
        $listener.Start()
    } catch {
        throw "[LocalOCR] Cannot bind ${HostAddress}:$Port before WSL startup. Check Windows excluded port ranges and current listeners. $($_.Exception.Message)"
    } finally {
        if ($null -ne $listener) {
            $listener.Stop()
        }
    }
}

$lastHealthError = $null

function Wait-LocalOcrHealth {
    param(
        [datetime]$Deadline
    )

    $script:lastHealthError = $null
    do {
        Start-Sleep -Seconds 2
        try {
            $health = Invoke-RestMethod -Uri "http://${HostAddress}:$Port/health" -Method Get -TimeoutSec 15
            if (Assert-LocalOcrHealthPayload -Health $health) {
                return $health
            }
        } catch {
            if ($_.Exception.Message -like "*non-LocalOCR service*") {
                throw
            }
            $script:lastHealthError = $_
        }
    } while ((Get-Date) -lt $Deadline)
    return $null
}

function Write-LocalOcrStartupDiagnostics {
    param(
        [string]$Message
    )

    Write-Host $Message -ForegroundColor Yellow
    if (Test-Path -LiteralPath $LogPath) {
        Write-Host "[LocalOCR] Last log lines:" -ForegroundColor Yellow
        Get-Content -LiteralPath $LogPath -Tail 40
    }
    if ($script:lastHealthError) {
        Write-Host "[LocalOCR] Last health-check error: $($script:lastHealthError.Exception.Message)" -ForegroundColor Yellow
    }
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

    $existingProcess = Get-LocalOcrProcess
    if ($existingProcess) {
        Write-Host "[LocalOCR] API startup already in progress (PID $($existingProcess.Id)); waiting up to ${StartupTimeoutSec}s ..." -ForegroundColor Cyan
        $deadline = (Get-Date).AddSeconds($StartupTimeoutSec)
        $health = Wait-LocalOcrHealth -Deadline $deadline
        if ($health) {
            Write-Host "[LocalOCR] API ready. GPU: $($health.gpu)" -ForegroundColor Green
            return
        }
        Write-LocalOcrStartupDiagnostics -Message "[LocalOCR] Server did not become ready before ${StartupTimeoutSec}s timeout."
        throw "LocalOCR API startup timed out while existing process is still running."
    }

    Assert-LocalOcrPortBindable

    Write-Host "[LocalOCR] Starting API server at http://${HostAddress}:$Port ..." -ForegroundColor Cyan
    Write-Host "[LocalOCR] Log: $LogPath" -ForegroundColor DarkGray
    $wslLogPath = "/mnt/e/Projects/Tools/LocalOCR/_server/localocr-api.log"
    $wslCommand = "cd /mnt/e/Projects/Tools/LocalOCR && exec scripts/run_in_wsl.sh -m localocr.server --host '$HostAddress' --port $Port >> '$wslLogPath' 2>&1"
    $escapedCommand = $wslCommand.Replace('"', '\"')
    $arguments = "-d Ubuntu -e bash -lc `"$escapedCommand`""
    $process = Start-Process -FilePath "wsl.exe" -ArgumentList $arguments -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath $PidPath -Value $process.Id -Encoding ascii

    $deadline = (Get-Date).AddSeconds($StartupTimeoutSec)
    $health = Wait-LocalOcrHealth -Deadline $deadline
    if ($health) {
        Write-Host "[LocalOCR] API ready. GPU: $($health.gpu)" -ForegroundColor Green
        return
    }
    Write-LocalOcrStartupDiagnostics -Message "[LocalOCR] Server did not become ready before ${StartupTimeoutSec}s timeout."
    throw "LocalOCR API server startup timed out."
} finally {
    if ($hasMutex) {
        $mutex.ReleaseMutex() | Out-Null
    }
    $mutex.Dispose()
}
