# Smart LocalOCR wrapper for Codex. It never waits forever on an OCR client call.
param(
    [Parameter(Position = 0)]
    [string]$Path,

    [ValidateSet("auto", "ocr", "vl", "structure")]
    [string]$Engine = "auto",
    [string]$Model,

    [switch]$Recursive,
    [string]$OutDir,
    [int]$Port = 18665,
    [ValidateSet("127.0.0.1")][string]$HostAddress = "127.0.0.1",
    [int]$TimeoutSec = 3600,
    [ValidateRange(1, 7200)]
    [int]$ExecutionTimeoutSec = 300,
    [int]$StartupTimeoutSec = 600,
    [int]$OuterTimeoutSec = 330,
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

    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $FilePath
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    foreach ($argument in $ArgumentList) {
        [void]$psi.ArgumentList.Add([string]$argument)
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $psi
    try {
        if (-not $process.Start()) {
            return [pscustomobject]@{
                timed_out = $false
                start_failed = $true
                exit_code = $null
                stdout = ""
                stderr = "Process did not start."
            }
        }
    } catch {
        return [pscustomobject]@{
            timed_out = $false
            start_failed = $true
            exit_code = $null
            stdout = ""
            stderr = $_.Exception.Message
        }
    }

    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    try {
        $completed = $process.WaitForExit($TimeoutSec * 1000)
        if (-not $completed) {
            try {
                $process.Kill()
            } catch {
                try {
                    $process.Kill()
                } catch {
                }
            }
            [void]$process.WaitForExit(5000)
            return [pscustomobject]@{
                timed_out = $true
                start_failed = $false
                exit_code = $null
                stdout = if ($stdoutTask.Wait(1000)) { $stdoutTask.GetAwaiter().GetResult() } else { "" }
                stderr = if ($stderrTask.Wait(1000)) { $stderrTask.GetAwaiter().GetResult() } else { "Output pipe remained open after the client deadline." }
            }
        }

        # Finalize redirected streams before reading ExitCode; otherwise a
        # completed child can be misreported with a stale/null state.
        [void]$process.WaitForExit(5000)
        $process.Refresh()
        $exitCode = [int]$process.ExitCode
        return [pscustomobject]@{
            timed_out = $false
            start_failed = $false
            exit_code = $exitCode
            stdout = if ($stdoutTask.Wait(1000)) { $stdoutTask.GetAwaiter().GetResult() } else { "" }
            stderr = if ($stderrTask.Wait(1000)) { $stderrTask.GetAwaiter().GetResult() } else { "Output pipe remained open after the client exited." }
        }
    } finally {
        $process.Dispose()
    }
}

function Get-LocalOcrHealth {
    param(
        [int]$Port,
        [string]$HostAddress
    )

    try {
        $health = Invoke-RestMethod -Uri "http://${HostAddress}:$Port/health" -Method Get -TimeoutSec 5
        $names = @($health.PSObject.Properties.Name)
        if ($health.ok -ne $true) {
            return [pscustomobject]@{
                ok = $false
                health_reachable = $true
                health_active_jobs_supported = $names -contains "active_jobs"
                error = "non_localocr_health_response"
                response = $health
            }
        }
        $health | Add-Member -NotePropertyName health_reachable -NotePropertyValue $true -Force
        $health | Add-Member -NotePropertyName health_active_jobs_supported -NotePropertyValue ($names -contains "active_jobs") -Force
        return $health
    } catch {
        return [pscustomobject]@{
            ok = $false
            health_reachable = $false
            health_active_jobs_supported = $false
            error = $_.Exception.Message
        }
    }
}

function Get-LocalOcrActivity {
    param([Parameter(Mandatory = $true)]$Health)

    $names = @($Health.PSObject.Properties.Name)
    $reachable = ($Health.health_reachable -eq $true) -or ($Health.ok -eq $true)
    $supports = $names -contains "active_jobs"
    $jobs = @()
    if ($supports -and $null -ne $Health.active_jobs) {
        $jobs = @($Health.active_jobs | Where-Object {
                $null -ne $_ -and ([string]$_.status -eq "running")
            })
    }
    $count = $null
    if ($supports -and $names -contains "active_jobs_count" -and $null -ne $Health.active_jobs_count) {
        try {
            $count = [int]$Health.active_jobs_count
        } catch {
            $count = $null
        }
    }
    if ($supports -and $null -eq $count -and $null -ne $Health.active_jobs) {
        $count = $jobs.Count
    }

    if (-not $reachable) {
        $state = "unavailable"
        $reason = "health_unavailable"
    } elseif (-not $supports -or $null -eq $Health.active_jobs -or $null -eq $count) {
        # A legacy/partial health payload is not evidence of an idle service.
        $state = "unknown"
        $reason = "health_active_jobs_unavailable"
    } elseif ($count -gt 0 -or $jobs.Count -gt 0) {
        $state = "active"
        $reason = "api_active_jobs"
    } else {
        $state = "idle"
        $reason = "api_active_jobs_empty"
    }

    return [pscustomobject]@{
        state = $state
        reason = $reason
        active_jobs_supported = $supports
        active_jobs_count = $count
        active_jobs = $jobs
    }
}

function Get-FirstActiveJob {
    param([AllowNull()]$Activity)
    if ($null -eq $Activity -or $null -eq $Activity.active_jobs) {
        return $null
    }
    return @($Activity.active_jobs)[0]
}

function Add-JobLocationFields {
    param(
        [Parameter(Mandatory = $true)]$Fields,
        [AllowNull()]$Job
    )
    if ($null -eq $Job) {
        return
    }
    foreach ($name in @("job_id", "job_key", "stage", "engine", "model_id", "source_file", "started_at", "updated_at", "timeout_sec", "deadline_at", "manifest")) {
        if (@($Job.PSObject.Properties.Name) -contains $name) {
            $Fields[$name] = $Job.$name
        }
    }
}

function Add-SmartMetadata {
    param(
        [Parameter(Mandatory = $true)]$Payload,
        [Parameter(Mandatory = $true)]$Route,
        [AllowNull()]$Activity,
        [int]$ClientExitCode = 0
    )
    $Payload | Add-Member -NotePropertyName smart -NotePropertyValue ([pscustomobject]@{
            requested_engine = $Engine
            requested_model = $Model
            preview_effective_engine = $Route.effective_engine
            preview_route_reason = $Route.reason
            preview_route_confidence = $Route.confidence
            preview_route_signals = $Route.signals
            route_reason = $Route.reason
            outer_timeout_sec = $OuterTimeoutSec
            execution_timeout_sec = $ExecutionTimeoutSec
            client_exit_code = $ClientExitCode
            active_state = if ($Activity) { $Activity.state } else { $null }
            active_state_reason = if ($Activity) { $Activity.reason } else { $null }
        }) -Force
    return $Payload
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

function Read-ChildJsonPayload {
    param([AllowNull()][string]$Text)
    $jsonText = Select-JsonObjectText $Text
    if (-not $jsonText) {
        return $null
    }
    try {
        return $jsonText | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return $null
    }
}

function New-ActiveTaskPayload {
    param(
        [Parameter(Mandatory = $true)]$Activity,
        [Parameter(Mandatory = $true)]$Health
    )
    $fields = [ordered]@{
        ok = $false
        status = "active_localocr_task"
        http_status = 409
        error_code = "localocr_busy"
        recommendation = "do_not_blindly_retry"
        active_state = $Activity.state
        active_state_reason = $Activity.reason
        active_jobs_count = $Activity.active_jobs_count
        active_jobs = $Activity.active_jobs
        health = $Health
    }
    Add-JobLocationFields -Fields $fields -Job (Get-FirstActiveJob $Activity)
    return [pscustomobject]$fields
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
    try {
        $route = Resolve-SmartRoutePreview -InputPath $Path -RequestedEngine $Engine -RequestedModel $Model
    } catch {
        $route = [pscustomobject]@{
            effective_engine = $Engine
            reason = "route_preview_failed"
            confidence = $null
            signals = @()
        }
    }
} else {
    $route = [pscustomobject]@{
        effective_engine = $null
        reason = "not_applicable_without_path"
        confidence = $null
        signals = @()
    }
}

$health = Get-LocalOcrHealth -Port $Port -HostAddress $HostAddress
$activity = Get-LocalOcrActivity -Health $health

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
        active_state = $activity.state
        active_state_reason = $activity.reason
        health_active_jobs_supported = $activity.active_jobs_supported
        active_jobs_count = $activity.active_jobs_count
        active_jobs = $activity.active_jobs
        health = $health
    })
    exit 0
}

if (($activity.state -eq "active") -and (-not $Force)) {
    $payload = New-ActiveTaskPayload -Activity $activity -Health $health
    $payload = Add-SmartMetadata -Payload $payload -Route $route -Activity $activity -ClientExitCode 75
    $payload | ConvertTo-Json -Depth 100
    exit 75
}

if (($activity.state -eq "unknown") -and (-not $Force)) {
    $payload = [pscustomobject]@{
        ok = $false
        status = "readiness_unknown"
        error_code = "health_active_jobs_unavailable"
        recommendation = "inspect_health_and_active_jobs_before_retry"
        detail = "LocalOCR health did not expose a reliable active_jobs state; the wrapper will not treat that as idle."
        active_state = $activity.state
        active_state_reason = $activity.reason
        health_active_jobs_supported = $activity.active_jobs_supported
        active_jobs_count = $activity.active_jobs_count
        active_jobs = $activity.active_jobs
        health = $health
    }
    $payload = Add-SmartMetadata -Payload $payload -Route $route -Activity $activity -ClientExitCode 1
    $payload | ConvertTo-Json -Depth 100
    exit 1
}

$ocrOnce = Join-Path $ScriptDir "ocr_once.ps1"
$childInvocation = "& $(Quote-PowerShellString $ocrOnce) $(Quote-PowerShellString $Path) -Engine $(Quote-PowerShellString $Engine) -Port $Port -HostAddress $(Quote-PowerShellString $HostAddress) -TimeoutSec $TimeoutSec -ExecutionTimeoutSec $ExecutionTimeoutSec -StartupTimeoutSec $StartupTimeoutSec"
if ($Model) {
    $childInvocation += " -Model $(Quote-PowerShellString $Model)"
}
if ($Recursive) {
    $childInvocation += " -Recursive"
}
if ($OutDir) {
    $childInvocation += " -OutDir $(Quote-PowerShellString $OutDir)"
}
if ($StopAfter) {
    $childInvocation += " -StopAfter"
}

$commandParts = @(
    "`$ErrorActionPreference = 'Stop'",
    "`$ProgressPreference = 'SilentlyContinue'",
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
    $childInvocation,
    "exit `$LASTEXITCODE"
)
$encodedCommand = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes(($commandParts -join "`n")))
$psExe = (Get-Process -Id $PID).Path
$client = Invoke-BoundedProcess -FilePath $psExe -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encodedCommand) -TimeoutSec $OuterTimeoutSec

if ($client.timed_out) {
    $healthAfterTimeout = Get-LocalOcrHealth -Port $Port -HostAddress $HostAddress
    $activityAfterTimeout = Get-LocalOcrActivity -Health $healthAfterTimeout
    $fields = [ordered]@{
        ok = $false
        status = "client_timeout"
        error_code = "client_timeout"
        recommendation = "do_not_blindly_retry"
        requested_engine = $Engine
        requested_model = $Model
        effective_engine = $route.effective_engine
        route_reason = $route.reason
        route_confidence = $route.confidence
        route_signals = $route.signals
        outer_timeout_sec = $OuterTimeoutSec
        execution_timeout_sec = $ExecutionTimeoutSec
        active_state = $activityAfterTimeout.state
        active_state_reason = $activityAfterTimeout.reason
        active_jobs_count = $activityAfterTimeout.active_jobs_count
        active_jobs = $activityAfterTimeout.active_jobs
        stdout_tail = Get-TextTail $client.stdout
        stderr_tail = Get-TextTail $client.stderr
        health = $healthAfterTimeout
    }
    Add-JobLocationFields -Fields $fields -Job (Get-FirstActiveJob $activityAfterTimeout)
    ConvertTo-CompactJson ([pscustomobject]$fields)
    exit 124
}

$childPayload = Read-ChildJsonPayload $client.stdout
if ($null -ne $childPayload) {
    $childPayload = Add-SmartMetadata -Payload $childPayload -Route $route -Activity $activity -ClientExitCode $(if ($null -eq $client.exit_code) { 1 } else { $client.exit_code })
    $childPayload | ConvertTo-Json -Depth 100
    if ($null -eq $client.exit_code) {
        exit 1
    }
    exit ([int]$client.exit_code)
}

$failure = [pscustomobject]@{
    ok = $false
    status = "client_failed"
    error_code = "client_failed"
    recommendation = "inspect_stderr_and_health"
    requested_engine = $Engine
    requested_model = $Model
    effective_engine = $route.effective_engine
    route_reason = $route.reason
    route_confidence = $route.confidence
    route_signals = $route.signals
    client_exit_code = $client.exit_code
    stdout_tail = Get-TextTail $client.stdout
    stderr_tail = Get-TextTail $client.stderr
    health = $health
}
$failure = Add-SmartMetadata -Payload $failure -Route $route -Activity $activity -ClientExitCode $(if ($null -eq $client.exit_code) { 1 } else { $client.exit_code })
$failure | ConvertTo-Json -Depth 100
exit 1
