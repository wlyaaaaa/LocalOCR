# Stop LocalOCR local API server processes inside WSL.
param(
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerDir = Join-Path $ScriptDir "_server"
$PidPath = Join-Path $ServerDir "wsl-server.pid"

function Get-WslPids {
    param([string]$Pattern)

    $raw = & wsl.exe -d Ubuntu -e bash -lc "pgrep -f '$Pattern' || true"
    $pids = @()
    foreach ($line in $raw) {
        $pidText = ($line | Out-String).Trim()
        if ($pidText -match '^\d+$') {
            $pids += $pidText
        }
    }
    return $pids
}

function Send-WslSignal {
    param(
        [string[]]$Pids,
        [string]$Signal
    )

    foreach ($pidText in $Pids) {
        if ($pidText -match '^\d+$') {
            & wsl.exe -d Ubuntu -e bash -lc "kill -$Signal $pidText 2>/dev/null || true" | Out-Null
        }
    }
}

Write-Host "[LocalOCR] Stopping API server on port $Port ..." -ForegroundColor Cyan

# Clean up API-spawned localocr.cli VL subprocesses before stopping the API server.
$patterns = @(
    "^/root/localocr-venv/bin/python -m [l]ocalocr.cli .*_pdf_pages/api/vl_subprocess",
    "^/root/localocr-venv/bin/python -m [l]ocalocr.server"
)

foreach ($pattern in $patterns) {
    Send-WslSignal -Pids (Get-WslPids -Pattern $pattern) -Signal "TERM"
}

Start-Sleep -Seconds 2

foreach ($pattern in $patterns) {
    Send-WslSignal -Pids (Get-WslPids -Pattern $pattern) -Signal "KILL"
}

if (Test-Path -LiteralPath $PidPath) {
    Remove-Item -LiteralPath $PidPath -Force
}

Write-Host "[LocalOCR] Stop command sent." -ForegroundColor Green
