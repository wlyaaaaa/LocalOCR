# Smart LocalOCR wrapper for Codex. It never waits forever on an OCR client call.
param(
    [Parameter(Position = 0)]
    [string]$Path,

    [ValidateSet("auto", "ocr", "vl", "structure")]
    [string]$Engine = "auto",
    [string]$Model,

    [switch]$Recursive,
    [string]$OutDir,
    [int]$Port = 8765,
    [string]$HostAddress = "127.0.0.1",
    [int]$TimeoutSec = 3600,
    [int]$StartupTimeoutSec = 600,
    [int]$OuterTimeoutSec = 120,
    [switch]$StopAfter,
    [switch]$Force,
    [switch]$TriageOnly
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function ConvertTo-CompactJson {
    param([Parameter(Mandatory = $true)]$Value)
    $Value | ConvertTo-Json -Depth 100 -Compress
}

function Get-TextTail {
    param(
        [AllowNull()][string]$Text,
        [int]$MaxChars = 2000
    )
    if (-not $Text) {
        return ""
    }
    if ($Text.Length -le $MaxChars) {
        return $Text
    }
    return $Text.Substring($Text.Length - $MaxChars)
}

function Quote-PowerShellString {
    param([AllowNull()][string]$Value)
    if ($null -eq $Value) {
        return "''"
    }
    return "'" + ($Value -replace "'", "''") + "'"
}

function Resolve-SmartRoutePreview {
    param(
        [Parameter(Mandatory = $true)][string]$InputPath,
        [Parameter(Mandatory = $true)][string]$RequestedEngine,
        [AllowNull()][string]$RequestedModel
    )

    if ($RequestedModel) {
        return [pscustomobject]@{
            effective_engine = $RequestedEngine
            reason = "explicit_model"
            confidence = 1.0
            signals = @("explicit_model")
        }
    }

    if ($RequestedEngine -ne "auto") {
        return [pscustomobject]@{
            effective_engine = $RequestedEngine
            reason = "explicit_$RequestedEngine"
            confidence = 1.0
            signals = @("explicit_engine")
        }
    }

    $extension = [System.IO.Path]::GetExtension($InputPath).ToLowerInvariant()
    $fileName = [System.IO.Path]::GetFileName($InputPath).ToLowerInvariant()
    $complexKeywords = @("table", "formula", "layout", "multi", "column", "lecture", "paper", "论文", "公式", "表格", "多栏", "课件")

    if ($extension -in @(".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")) {
        return [pscustomobject]@{
            effective_engine = "ocr"
            reason = "image_prefers_ocr"
            confidence = 0.9
            signals = @("image", "ext:$extension")
        }
    }

    if ($extension -eq ".pdf") {
        $signals = @("pdf", "ext:.pdf")
        foreach ($keyword in $complexKeywords) {
            if ($fileName.Contains($keyword)) {
                $signals += "complex_keyword:$keyword"
            }
        }
        if ($signals.Count -gt 2) {
            return [pscustomobject]@{
                effective_engine = "vl"
                reason = "pdf_complex_layout_prefers_vl"
                confidence = 0.82
                signals = $signals
            }
        }
        $signals += "plain_pdf_default"
        return [pscustomobject]@{
            effective_engine = "ocr"
            reason = "pdf_plain_text_prefers_ocr"
            confidence = 0.72
            signals = $signals
        }
    }

    return [pscustomobject]@{
        effective_engine = "ocr"
        reason = "unknown_type_prefers_ocr"
        confidence = 0.5
        signals = @("unknown_type", "ext:$extension")
    }
}

function Invoke-BoundedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [int]$TimeoutSec = 10
    )

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
        $completed = $process.WaitForExit($TimeoutSec * 1000)
        if (-not $completed) {
            try {
                $process.Kill()
            } catch {
            }
            return [pscustomobject]@{
                timed_out = $true
                exit_code = $null
                stdout = Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue
                stderr = Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue
            }
        }

        $process.WaitForExit()
        $process.Refresh()
        $exitCode = [int]$process.ExitCode

        return [pscustomobject]@{
            timed_out = $false
            exit_code = $exitCode
            stdout = Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue
            stderr = Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue
        }
    } finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-WslBashBounded {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [int]$TimeoutSec = 10
    )

    $arguments = @("-d", "Ubuntu", "-e", "bash", "-lc", $Command)
    return Invoke-BoundedProcess -FilePath "wsl.exe" -ArgumentList $arguments -TimeoutSec $TimeoutSec
}

function Get-LocalOcrActiveTasks {
    $probe = Invoke-WslBashBounded -Command "timeout 8s pgrep -af '[l]ocalocr[.]cli|[v]l_subprocess|[s]tructure_subprocess' || true" -TimeoutSec 10
    if ($probe.timed_out) {
        return [pscustomobject]@{
            timed_out = $true
            tasks = @()
            stderr = Get-TextTail $probe.stderr
        }
    }

    $lines = @()
    if ($probe.stdout) {
        $lines = @($probe.stdout -split "`r?`n" | Where-Object { $_.Trim() })
    }

    return [pscustomobject]@{
        timed_out = $false
        tasks = $lines
        stderr = Get-TextTail $probe.stderr
    }
}

function Get-LocalOcrHealthCompact {
    param(
        [int]$Port,
        [string]$HostAddress
    )

    try {
        return Invoke-RestMethod -Uri "http://${HostAddress}:$Port/health" -Method Get -TimeoutSec 5
    } catch {
        return [pscustomobject]@{
            ok = $false
            error = $_.Exception.Message
        }
    }
}

function Select-JsonObjectText {
    param([AllowNull()][string]$Text)
    if (-not $Text) {
        return $null
    }
    $start = $Text.IndexOf("{")
    $end = $Text.LastIndexOf("}")
    if ($start -lt 0 -or $end -lt $start) {
        return $null
    }
    return $Text.Substring($start, $end - $start + 1)
}

if (-not $Path -and -not $TriageOnly) {
    ConvertTo-CompactJson ([pscustomobject]@{
        ok = $false
        status = "missing_path"
        recommendation = "supply_path_or_use_triage_only"
    })
    exit 0
}

if ($Path) {
    $route = Resolve-SmartRoutePreview -InputPath $Path -RequestedEngine $Engine -RequestedModel $Model
} else {
    $route = [pscustomobject]@{
        effective_engine = $null
        reason = "not_applicable_without_path"
        confidence = $null
        signals = @()
    }
}
$active = Get-LocalOcrActiveTasks
$health = Get-LocalOcrHealthCompact -Port $Port -HostAddress $HostAddress

if ($TriageOnly) {
    ConvertTo-CompactJson ([pscustomobject]@{
        ok = $true
        status = "triage_only"
        requested_engine = $Engine
        requested_model = $Model
        effective_engine = $route.effective_engine
        route_reason = $route.reason
        route_confidence = $route.confidence
        route_signals = $route.signals
        active_tasks_count = @($active.tasks).Count
        active_tasks = $active.tasks
        active_probe_timed_out = $active.timed_out
        health = $health
    })
    exit 0
}

if ((@($active.tasks).Count -gt 0) -and (-not $Force)) {
    ConvertTo-CompactJson ([pscustomobject]@{
        ok = $false
        status = "active_localocr_task"
        recommendation = "do_not_blindly_retry"
        requested_engine = $Engine
        requested_model = $Model
        effective_engine = $route.effective_engine
        route_reason = $route.reason
        route_confidence = $route.confidence
        route_signals = $route.signals
        active_tasks_count = @($active.tasks).Count
        active_tasks = $active.tasks
        health = $health
    })
    exit 0
}

$ocrOnce = Join-Path $ScriptDir "ocr_once.ps1"
$commandParts = @(
    "`$ErrorActionPreference = 'Stop'",
    "`$ProgressPreference = 'SilentlyContinue'",
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
    "& $(Quote-PowerShellString $ocrOnce) $(Quote-PowerShellString $Path) -Engine $(Quote-PowerShellString $Engine) -Port $Port -HostAddress $(Quote-PowerShellString $HostAddress) -TimeoutSec $TimeoutSec -StartupTimeoutSec $StartupTimeoutSec"
)
if ($Model) {
    $commandParts[-1] += " -Model $(Quote-PowerShellString $Model)"
}
if ($Recursive) {
    $commandParts[-1] += " -Recursive"
}
if ($OutDir) {
    $commandParts[-1] += " -OutDir $(Quote-PowerShellString $OutDir)"
}
if ($StopAfter) {
    $commandParts[-1] += " -StopAfter"
}

$encodedCommand = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes(($commandParts -join "`n")))
$psExe = (Get-Process -Id $PID).Path
$client = Invoke-BoundedProcess -FilePath $psExe -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encodedCommand) -TimeoutSec $OuterTimeoutSec

if ($client.timed_out) {
    $activeAfterTimeout = Get-LocalOcrActiveTasks
    ConvertTo-CompactJson ([pscustomobject]@{
        ok = $false
        status = "client_timeout"
        recommendation = "do_not_blindly_retry"
        requested_engine = $Engine
        requested_model = $Model
        effective_engine = $route.effective_engine
        route_reason = $route.reason
        route_confidence = $route.confidence
        route_signals = $route.signals
        outer_timeout_sec = $OuterTimeoutSec
        active_tasks_count = @($activeAfterTimeout.tasks).Count
        active_tasks = $activeAfterTimeout.tasks
        stdout_tail = Get-TextTail $client.stdout
        stderr_tail = Get-TextTail $client.stderr
        health = (Get-LocalOcrHealthCompact -Port $Port -HostAddress $HostAddress)
    })
    exit 0
}

if ($client.exit_code -ne 0) {
    ConvertTo-CompactJson ([pscustomobject]@{
        ok = $false
        status = "client_failed"
        recommendation = "inspect_stderr_and_health"
        requested_engine = $Engine
        requested_model = $Model
        effective_engine = $route.effective_engine
        route_reason = $route.reason
        route_confidence = $route.confidence
        route_signals = $route.signals
        exit_code = $client.exit_code
        stdout_tail = Get-TextTail $client.stdout
        stderr_tail = Get-TextTail $client.stderr
        health = (Get-LocalOcrHealthCompact -Port $Port -HostAddress $HostAddress)
    })
    exit 0
}

$jsonText = Select-JsonObjectText $client.stdout
if (-not $jsonText) {
    ConvertTo-CompactJson ([pscustomobject]@{
        ok = $false
        status = "client_output_parse_failed"
        recommendation = "inspect_stdout_tail"
        requested_engine = $Engine
        requested_model = $Model
        effective_engine = $route.effective_engine
        route_reason = $route.reason
        route_confidence = $route.confidence
        route_signals = $route.signals
        stdout_tail = Get-TextTail $client.stdout
        stderr_tail = Get-TextTail $client.stderr
    })
    exit 0
}

try {
    $result = $jsonText | ConvertFrom-Json
    $result | Add-Member -NotePropertyName smart -NotePropertyValue ([pscustomobject]@{
        requested_engine = $Engine
        requested_model = $Model
        preview_effective_engine = $route.effective_engine
        preview_route_reason = $route.reason
        preview_route_confidence = $route.confidence
        preview_route_signals = $route.signals
        route_reason = $route.reason
        outer_timeout_sec = $OuterTimeoutSec
    }) -Force
    $result | ConvertTo-Json -Depth 100
} catch {
    ConvertTo-CompactJson ([pscustomobject]@{
        ok = $false
        status = "client_output_parse_failed"
        recommendation = "inspect_stdout_tail"
        requested_engine = $Engine
        requested_model = $Model
        effective_engine = $route.effective_engine
        route_reason = $route.reason
        route_confidence = $route.confidence
        route_signals = $route.signals
        error = $_.Exception.Message
        stdout_tail = Get-TextTail $client.stdout
        stderr_tail = Get-TextTail $client.stderr
    })
    exit 0
}
