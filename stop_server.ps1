# Stop LocalOCR local API server processes inside WSL.
param(
    [int]$Port = 18665,
    [int]$WslTimeoutSec = 10
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerDir = Join-Path $ScriptDir "_server"
$PidPath = Join-Path $ServerDir "wsl-server.pid"

function Invoke-WslCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [int]$TimeoutSec = $WslTimeoutSec
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'wsl.exe'
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    foreach ($arg in $Arguments) {
        [void]$psi.ArgumentList.Add($arg)
    }

    $proc = [System.Diagnostics.Process]::Start($psi)
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        } catch {
            # Best-effort cleanup only; caller receives timeout warning.
        }
        throw "wsl.exe timed out after ${TimeoutSec}s: $($Arguments -join ' ')"
    }

    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    if ($proc.ExitCode -ne 0 -and $stderr.Trim()) {
        Write-Verbose $stderr
    }
    return $stdout
}

function Get-WslPids {
    param([string]$Pattern)

    $raw = Invoke-WslCommand -Arguments @('-d', 'Ubuntu', '-e', 'bash', '-lc', "pgrep -f '$Pattern' || true")
    $pids = @()
    foreach ($line in ($raw -split "`r?`n")) {
        $pidText = $line.Trim()
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
            Invoke-WslCommand -Arguments @('-d', 'Ubuntu', '-e', 'bash', '-lc', "kill -$Signal $pidText 2>/dev/null || true") | Out-Null
        }
    }
}

Write-Host "[LocalOCR] Stopping API server on port $Port ..." -ForegroundColor Cyan

# Clean up API-spawned heavy localocr.cli subprocesses before stopping the API server.
$patterns = @(
    "^/root/localocr-venv/bin/python -m [l]ocalocr.cli .*_pdf_pages/api/vl_subprocess",
    "^/root/localocr-venv/bin/python -m [l]ocalocr.cli .*_pdf_pages/api/structure_subprocess",
    "^/root/localocr-venv/bin/python -m [l]ocalocr.server"
)

try {
    foreach ($pattern in $patterns) {
        Send-WslSignal -Pids (Get-WslPids -Pattern $pattern) -Signal "TERM"
    }

    Start-Sleep -Seconds 2

    foreach ($pattern in $patterns) {
        Send-WslSignal -Pids (Get-WslPids -Pattern $pattern) -Signal "KILL"
    }
} catch {
    Write-Warning "LocalOCR WSL cleanup did not complete: $($_.Exception.Message)"
}

if (Test-Path -LiteralPath $PidPath) {
    Remove-Item -LiteralPath $PidPath -Force
}

Write-Host "[LocalOCR] Stop command sent." -ForegroundColor Green
