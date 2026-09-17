# Stop exactly one LocalOCR API server and its supervised worker tree inside WSL.
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 18665,
    [ValidateSet("127.0.0.1")][string]$HostAddress = "127.0.0.1",
    [ValidateRange(1, 10)]
    [int]$GraceSec = 5,
    [ValidateRange(1, 10)]
    [int]$WslTimeoutSec = 10
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerDir = Join-Path $ScriptDir "_server"
# Legacy cleanup only; this file is never the current server identity source.
$PidPath = Join-Path $ServerDir "wsl-server.pid"
$RuntimeScript = "/mnt/e/Projects/Tools/LocalOCR/scripts/run_in_wsl.sh"

# This query is intentionally a fixed, one-shot psutil lookup. It does not
# import LocalOCR/Paddle, scan by a broad process name, or stop another port.
$SnapshotCode = @'
import json
import os
import sys

import psutil


def port_number(argv):
    for index, value in enumerate(argv[:-1]):
        if value == "--port":
            try:
                return int(argv[index + 1])
            except (TypeError, ValueError):
                return None
    return None


def same_project(cwd):
    if not cwd:
        return False
    return os.path.normcase(os.path.realpath(cwd)).rstrip("/") == "/mnt/e/Projects/Tools/LocalOCR"


def process_record(process):
    try:
        info = process.as_dict(attrs=["pid", "ppid", "create_time", "cmdline", "cwd"])
    except (psutil.Error, OSError):
        return None
    info["cmdline"] = info.get("cmdline") or []
    return info


port = int(sys.argv[1])
expected_pid = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] != "-" else None
expected_start = float(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] != "-" else None
servers = []
for process in psutil.process_iter():
    record = process_record(process)
    if not record or not same_project(record.get("cwd")):
        continue
    command = record["cmdline"]
    if "localocr.server" not in command or port_number(command) != port:
        continue
    descendants = []
    try:
        descendants = [child for child in process.children(recursive=True)]
    except (psutil.Error, OSError):
        pass
    record["descendants"] = [item for child in descendants if (item := process_record(child))]
    servers.append(record)

listeners = []
try:
    connections = psutil.net_connections(kind="tcp")
except (psutil.Error, OSError):
    connections = []
for connection in connections:
    local = connection.laddr
    if connection.status != psutil.CONN_LISTEN or not local or local.port != port:
        continue
    listeners.append({"pid": connection.pid, "laddr": f"{local.ip}:{local.port}"})

identity_matches = []
if expected_pid is not None and expected_start is not None:
    for server in servers:
        if server.get("pid") != expected_pid:
            continue
        actual_start = server.get("create_time")
        if actual_start is not None and abs(float(actual_start) - expected_start) <= 0.01:
            identity_matches.append(server)

print(json.dumps({
    "port": port,
    "servers": servers,
    "listeners": listeners,
    "identity_matches": identity_matches,
}, ensure_ascii=False))
'@

function Invoke-WslCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [int]$TimeoutSec = $WslTimeoutSec
    )

    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = "wsl.exe"
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    foreach ($argument in $Arguments) {
        [void]$psi.ArgumentList.Add([string]$argument)
    }

    $process = [System.Diagnostics.Process]::Start($psi)
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    try {
        if (-not $process.WaitForExit($TimeoutSec * 1000)) {
            try {
                $process.Kill($true)
            } catch {
                try {
                    $process.Kill()
                } catch {
                }
            }
            # A WSL child can outlive the host process and retain inherited
            # pipe handles. Close our readers before throwing so callers do
            # not hang forever waiting for EOF from a detached child.
            try { $process.StandardOutput.Close() } catch { }
            try { $process.StandardError.Close() } catch { }
            throw "wsl.exe timed out after ${TimeoutSec}s: $($Arguments -join ' ')"
        }
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            $detail = if ($stderr.Trim()) { $stderr.Trim() } else { "exit=$($process.ExitCode)" }
            throw "wsl.exe failed: $detail"
        }
        return $stdout
    } finally {
        $process.Dispose()
    }
}

function Get-LocalOcrSnapshot {
    param(
        [AllowNull()][Nullable[int]]$ExpectedPid,
        [AllowNull()][Nullable[double]]$ExpectedStart
    )

    $pidArgument = if ($null -eq $ExpectedPid) { "-" } else { [string]$ExpectedPid }
    $startArgument = if ($null -eq $ExpectedStart) {
        "-"
    } else {
        $ExpectedStart.ToString([System.Globalization.CultureInfo]::InvariantCulture)
    }
    $raw = Invoke-WslCommand -Arguments @(
        "-d", "Ubuntu", "-e", "bash", $RuntimeScript, "-c", $SnapshotCode,
        [string]$Port, $pidArgument, $startArgument
    )
    try {
        return $raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "LocalOCR process snapshot returned invalid JSON: $($_.Exception.Message)"
    }
}

function Get-LocalOcrHealth {
    try {
        return Invoke-RestMethod -Uri "http://${HostAddress}:$Port/health" -Method Get -TimeoutSec 3
    } catch {
        return $null
    }
}

function Test-WindowsPortOccupied {
    # Avoid NetTCPIP enumeration: on some WSL-backed PowerShell hosts it can
    # block while querying mirrored listeners. Binding is bounded by the OS
    # and detects both WSL-forwarded and native listeners without a scan.
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $Port)
        $listener.Start()
        return $false
    } catch {
        return $true
    } finally {
        if ($null -ne $listener) {
            $listener.Stop()
        }
    }
}

function Get-ServerByPid {
    param(
        [Parameter(Mandatory = $true)]$Snapshot,
        [Parameter(Mandatory = $true)][int]$ServerPid,
        [AllowNull()][Nullable[double]]$ServerStart
    )
    $candidates = @($Snapshot.servers | Where-Object { [int]$_.pid -eq $ServerPid })
    if ($null -ne $ServerStart) {
        $candidates = @($candidates | Where-Object {
                $null -ne $_.create_time -and [math]::Abs(([double]$_.create_time) - $ServerStart) -le 0.01
            })
    }
    if ($candidates.Count -eq 1) {
        return $candidates[0]
    }
    return $null
}

function Assert-TargetOwnsPort {
    param(
        [Parameter(Mandatory = $true)]$Snapshot,
        [Parameter(Mandatory = $true)][int]$ServerPid
    )
    $listeners = @($Snapshot.listeners | Where-Object { $null -ne $_.pid -and [int]$_.pid -eq $ServerPid })
    if ($listeners.Count -eq 0) {
        throw "LocalOCR server PID $ServerPid does not own the target port $Port. Refusing to stop it."
    }
    $foreign = @($Snapshot.listeners | Where-Object { $null -ne $_.pid -and [int]$_.pid -ne $ServerPid })
    if ($foreign.Count -gt 0) {
        throw "Port $Port has a listener unrelated to LocalOCR. Refusing to stop any process."
    }
}

function Send-TargetSignal {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][ValidateSet("TERM", "KILL")][string]$Signal
    )
    try {
        Invoke-WslCommand -Arguments @(
            "-d", "Ubuntu", "-e", "kill", "-$Signal", [string]$ProcessId
        ) -TimeoutSec $WslTimeoutSec | Out-Null
        return $true
    } catch {
        # A process can exit between the identity snapshot and the signal. The
        # final identity/port verification decides whether this is success.
        return $false
    }
}

function Wait-TargetGone {
    param(
        [Parameter(Mandatory = $true)][int]$ServerPid,
        [Parameter(Mandatory = $true)][double]$ServerStart,
        [int]$WaitSec = $GraceSec
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($WaitSec)
    do {
        Start-Sleep -Milliseconds 250
        $snapshot = Get-LocalOcrSnapshot -ExpectedPid $ServerPid -ExpectedStart $ServerStart
        $target = Get-ServerByPid -Snapshot $snapshot -ServerPid $ServerPid -ServerStart $ServerStart
        if ($null -eq $target -and @($snapshot.listeners).Count -eq 0 -and -not (Test-WindowsPortOccupied)) {
            return $snapshot
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    return Get-LocalOcrSnapshot -ExpectedPid $ServerPid -ExpectedStart $ServerStart
}

function Remove-ServerPidFile {
    if (Test-Path -LiteralPath $PidPath) {
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction Stop
    }
}

Write-Host "[LocalOCR] Stopping API server on ${HostAddress}:$Port ..." -ForegroundColor Cyan

try {
    $health = Get-LocalOcrHealth
    if ($health -and $health.ok -eq $true -and $health.service -and $health.service -ne "localocr") {
        throw "Port $Port responded as service '$($health.service)', not LocalOCR. Refusing to stop it."
    }

    $serverPid = $null
    $serverStart = $null
    $healthHasIdentity = $false
    if ($health -and $health.ok -eq $true) {
        $healthNames = @($health.PSObject.Properties.Name)
        if ($healthNames -contains "server_pid" -and $healthNames -contains "server_start_time" -and $null -ne $health.server_pid -and $null -ne $health.server_start_time) {
            try {
                $serverPid = [int]$health.server_pid
                $serverStart = [double]$health.server_start_time
                $healthHasIdentity = $serverPid -gt 0 -and $serverStart -gt 0
            } catch {
                $healthHasIdentity = $false
            }
        }
    }

    $snapshot = Get-LocalOcrSnapshot -ExpectedPid $serverPid -ExpectedStart $serverStart
    if ($healthHasIdentity) {
        $target = Get-ServerByPid -Snapshot $snapshot -ServerPid $serverPid -ServerStart $serverStart
        if ($null -eq $target) {
            throw "LocalOCR health identity (pid=$serverPid, start=$serverStart) does not match the process on port $Port. Refusing to stop it."
        }
    } else {
        $servers = @($snapshot.servers)
        if ($servers.Count -eq 0 -and @($snapshot.listeners).Count -eq 0 -and -not (Test-WindowsPortOccupied)) {
            # Without a verified server identity this port may simply be an
            # already-unused alternate port; do not delete a PID file that
            # could belong to another LocalOCR instance.
            Write-Host "[LocalOCR] API is already stopped on port $Port." -ForegroundColor Green
            return
        }
        if ($servers.Count -ne 1) {
            throw "Could not identify exactly one legacy LocalOCR server for port $Port; refusing to stop it."
        }
        $target = $servers[0]
        $serverPid = [int]$target.pid
        $serverStart = [double]$target.create_time
    }

    Assert-TargetOwnsPort -Snapshot $snapshot -ServerPid $serverPid
    Write-Host "[LocalOCR] Sending TERM to verified server PID $serverPid (port $Port) ..." -ForegroundColor Cyan
    [void](Send-TargetSignal -ProcessId $serverPid -Signal "TERM")
    $afterTerm = Wait-TargetGone -ServerPid $serverPid -ServerStart $serverStart -WaitSec $GraceSec
    $stillRunning = Get-ServerByPid -Snapshot $afterTerm -ServerPid $serverPid -ServerStart $serverStart

    if ($null -ne $stillRunning) {
        # The API shutdown hook should stop its worker tree. If graceful
        # shutdown did not finish, kill only descendants captured under the
        # verified server identity, then the verified server itself.
        $descendants = @($stillRunning.descendants | Sort-Object pid -Descending)
        foreach ($child in $descendants) {
            [void](Send-TargetSignal -ProcessId ([int]$child.pid) -Signal "KILL")
        }
        [void](Send-TargetSignal -ProcessId $serverPid -Signal "KILL")
        $afterKill = Wait-TargetGone -ServerPid $serverPid -ServerStart $serverStart -WaitSec $GraceSec
        if ($null -ne (Get-ServerByPid -Snapshot $afterKill -ServerPid $serverPid -ServerStart $serverStart) -or @($afterKill.listeners).Count -gt 0 -or (Test-WindowsPortOccupied)) {
            throw "Verified LocalOCR server PID $serverPid did not stop cleanly; port $Port is still occupied."
        }
    } elseif (@($afterTerm.listeners).Count -gt 0 -or (Test-WindowsPortOccupied)) {
        throw "LocalOCR server PID $serverPid exited but port $Port remains occupied. Refusing to report release."
    }

    Remove-ServerPidFile
    Write-Host "[LocalOCR] API stopped on port $Port; verified worker tree cleanup." -ForegroundColor Green
    return
} catch {
    throw "[LocalOCR] Stop failed: $($_.Exception.Message)"
}
