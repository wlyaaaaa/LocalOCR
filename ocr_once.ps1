# Call the LocalOCR local API once. Starts the server if needed.
param(
    [Parameter(Mandatory = $true, Position = 0)]
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
    [switch]$StopAfter
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$base = "http://${HostAddress}:$Port"

function Read-JsonSafely {
    param([AllowNull()][string]$Raw)
    if (-not $Raw) {
        return $null
    }
    try {
        return $Raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return $null
    }
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

function Get-ResponseBody {
    param([AllowNull()][System.Net.WebResponse]$Response)
    if ($null -eq $Response) {
        return ""
    }

    $stream = $null
    $reader = $null
    try {
        $stream = $Response.GetResponseStream()
        if ($null -eq $stream) {
            return ""
        }
        $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8)
        return $reader.ReadToEnd()
    } finally {
        if ($null -ne $reader) {
            $reader.Dispose()
        } elseif ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Get-PropertyNames {
    param([AllowNull()]$Value)
    if ($null -eq $Value) {
        return @()
    }
    return @($Value.PSObject.Properties.Name)
}

function Get-WrapperExitCode {
    param(
        [AllowNull()]$Payload,
        [int]$HttpStatus = 0
    )

    if ($null -eq $Payload) {
        return 1
    }
    if ($Payload.ok -eq $true) {
        return 0
    }

    $status = [string]$Payload.status
    $errorCode = [string]$Payload.error_code
    if ($status -eq "active_localocr_task" -or $errorCode -in @("gpu_busy", "localocr_busy")) {
        return 75
    }
    if ($status -eq "client_timeout" -or $errorCode -in @("execution_timeout", "client_timeout")) {
        return 124
    }
    if ($errorCode -eq "missing_path" -or $HttpStatus -eq 404) {
        return 2
    }
    if ($errorCode -eq "broker_unavailable" -or $errorCode -eq "lease_lost" -or $HttpStatus -eq 503) {
        return 69
    }
    return 1
}

function New-HttpErrorPayload {
    param(
        [AllowNull()]$Payload,
        [AllowNull()][string]$Raw,
        [int]$HttpStatus,
        [AllowNull()][string]$TransportError
    )

    $fields = [ordered]@{}
    foreach ($name in (Get-PropertyNames $Payload)) {
        $fields[$name] = $Payload.$name
    }
    $fields["ok"] = $false

    if (-not $fields.Contains("status") -or -not $fields["status"]) {
        $fields["status"] = if ($HttpStatus -eq 409) { "active_localocr_task" } else { "failed" }
    }

    if (-not $fields.Contains("error_code") -or -not $fields["error_code"]) {
        $fields["error_code"] = switch ($HttpStatus) {
            400 { "input" }
            404 { "missing_path" }
            409 { "localocr_busy" }
            503 { "broker_unavailable" }
            504 { "execution_timeout" }
            500 { "runtime" }
            default { if ($HttpStatus -gt 0) { "http_$HttpStatus" } else { "request_failed" } }
        }
    }

    if (-not $fields.Contains("detail")) {
        if ($Raw) {
            $fields["detail"] = $Raw
        } elseif ($TransportError) {
            $fields["detail"] = $TransportError
        } else {
            $fields["detail"] = "LocalOCR request failed."
        }
    }
    if ($HttpStatus -gt 0 -and -not $fields.Contains("http_status")) {
        $fields["http_status"] = $HttpStatus
    }
    if ($TransportError -and -not $fields.Contains("transport_error")) {
        $fields["transport_error"] = $TransportError
    }
    if ($Raw -and $null -eq $Payload -and -not $fields.Contains("detail_raw")) {
        $fields["detail_raw"] = Get-TextTail $Raw
    }
    return [pscustomobject]$fields
}

function New-TransportTimeoutPayload {
    param([AllowNull()][string]$Detail)
    return [pscustomobject]@{
        ok = $false
        status = "client_timeout"
        error_code = "client_timeout"
        recommendation = "do_not_blindly_retry"
        detail = if ($Detail) { $Detail } else { "LocalOCR HTTP request timed out." }
    }
}

function Invoke-LocalOcrJsonRequest {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Json,
        [int]$TimeoutSec
    )

    $request = $null
    $response = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create($Uri)
        $request.Method = "POST"
        $request.ContentType = "application/json; charset=utf-8"
        $request.Timeout = $TimeoutSec * 1000
        $request.ReadWriteTimeout = $TimeoutSec * 1000
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Json)
        $request.ContentLength = $bytes.Length

        $stream = $request.GetRequestStream()
        try {
            $stream.Write($bytes, 0, $bytes.Length)
        } finally {
            $stream.Close()
        }

        $response = $request.GetResponse()
        $raw = Get-ResponseBody -Response $response
        $payload = Read-JsonSafely $raw
        if ($null -eq $payload) {
            $payload = New-HttpErrorPayload -Payload $null -Raw $raw -HttpStatus ([int]$response.StatusCode) -TransportError "LocalOCR returned invalid JSON."
        }
        return [pscustomobject]@{
            transport_ok = $true
            http_status = [int]$response.StatusCode
            raw = $raw
            payload = $payload
            transport_error = $null
        }
    } catch [System.Net.WebException] {
        $webException = $_.Exception
        $response = $webException.Response
        $httpStatus = 0
        if ($response -is [System.Net.HttpWebResponse]) {
            $httpStatus = [int]$response.StatusCode
        }
        $raw = Get-ResponseBody -Response $response
        $payload = Read-JsonSafely $raw
        if ($webException.Status -eq [System.Net.WebExceptionStatus]::Timeout -and $null -eq $payload) {
            $payload = New-TransportTimeoutPayload -Detail $webException.Message
        } else {
            $payload = New-HttpErrorPayload -Payload $payload -Raw $raw -HttpStatus $httpStatus -TransportError $webException.Message
        }
        return [pscustomobject]@{
            transport_ok = $false
            http_status = $httpStatus
            raw = $raw
            payload = $payload
            transport_error = $webException.Message
        }
    } catch {
        $message = $_.Exception.Message
        $payload = New-HttpErrorPayload -Payload $null -Raw "" -HttpStatus 0 -TransportError $message
        return [pscustomobject]@{
            transport_ok = $false
            http_status = 0
            raw = ""
            payload = $payload
            transport_error = $message
        }
    } finally {
        if ($null -ne $response) {
            $response.Close()
        }
    }
}

function Test-LocalOcrApi {
    try {
        $h = Invoke-RestMethod -Uri "$base/health" -Method Get -TimeoutSec 3
        return [bool]($h.ok -eq $true)
    } catch {
        return $false
    }
}

$exitCode = 0
$result = $null
$startupOutput = ""

try {
    if (-not (Test-LocalOcrApi)) {
        try {
            $startupLines = @(& (Join-Path $ScriptDir "start_server.ps1") -Port $Port -HostAddress $HostAddress -StartupTimeoutSec $StartupTimeoutSec 2>&1)
            $startupOutput = ($startupLines | ForEach-Object { [string]$_ }) -join "`n"
        } catch {
            throw "LocalOCR API startup failed: $($_.Exception.Message)"
        }
    }

    $body = [ordered]@{
        path = $Path
        engine = $Engine
        recursive = [bool]$Recursive
        write_outputs = $true
        timeout_sec = $ExecutionTimeoutSec
    }
    if ($Model) {
        $body.model = $Model
    }
    if ($OutDir) {
        $body.out_dir = $OutDir
    }

    $json = $body | ConvertTo-Json -Depth 8 -Compress
    $response = Invoke-LocalOcrJsonRequest -Uri "$base/ocr/path" -Json $json -TimeoutSec $TimeoutSec
    $result = $response.payload
    if ($null -eq $result) {
        $result = New-HttpErrorPayload -Payload $null -Raw $response.raw -HttpStatus $response.http_status -TransportError $response.transport_error
    }
    $exitCode = Get-WrapperExitCode -Payload $result -HttpStatus $response.http_status
} catch {
    $result = [pscustomobject]@{
        ok = $false
        status = "failed"
        error_code = "runtime"
        detail = $_.Exception.Message
    }
    $exitCode = 1
} finally {
    if ($StopAfter) {
        try {
            & (Join-Path $ScriptDir "stop_server.ps1") -Port $Port | Out-Null
        } catch {
            $result = [pscustomobject]@{
                ok = $false
                status = "cleanup_failed"
                error_code = "resource_release_failed"
                detail = $_.Exception.Message
                ocr_result = $result
            }
            $exitCode = 1
        }
    }
}

if ($startupOutput -and $result -and $result.ok -ne $true) {
    $result | Add-Member -NotePropertyName startup_output_tail -NotePropertyValue (Get-TextTail $startupOutput) -Force
}

$result | ConvertTo-Json -Depth 100
exit $exitCode
