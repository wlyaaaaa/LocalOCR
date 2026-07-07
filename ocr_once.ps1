# Call the LocalOCR local API once. Starts the server if needed.
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Path,

    [ValidateSet("auto", "ocr", "vl")]
    [string]$Engine = "auto",
    [string]$Model,

    [switch]$Recursive,
    [string]$OutDir,
    [int]$Port = 8765,
    [string]$HostAddress = "127.0.0.1",
    [int]$TimeoutSec = 3600,
    [int]$StartupTimeoutSec = 600,
    [switch]$StopAfter
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$base = "http://${HostAddress}:$Port"

function Test-LocalOcrApi {
    try {
        $h = Invoke-RestMethod -Uri "$base/health" -Method Get -TimeoutSec 3
        return [bool]$h.ok
    } catch {
        return $false
    }
}

try {
    if (-not (Test-LocalOcrApi)) {
        & (Join-Path $ScriptDir "start_server.ps1") -Port $Port -HostAddress $HostAddress -StartupTimeoutSec $StartupTimeoutSec
    }

    $body = @{
        path = $Path
        engine = $Engine
        recursive = [bool]$Recursive
        write_outputs = $true
    }
    if ($Model) {
        $body.model = $Model
    }
    if ($OutDir) {
        $body.out_dir = $OutDir
    }

    $json = $body | ConvertTo-Json -Depth 8
    $request = [System.Net.HttpWebRequest]::Create("$base/ocr/path")
    $request.Method = "POST"
    $request.ContentType = "application/json; charset=utf-8"
    $request.Timeout = $TimeoutSec * 1000
    $request.ReadWriteTimeout = $TimeoutSec * 1000
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $request.ContentLength = $bytes.Length
    $stream = $request.GetRequestStream()
    try {
        $stream.Write($bytes, 0, $bytes.Length)
    } finally {
        $stream.Close()
    }

    $response = $request.GetResponse()
    try {
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream(), [System.Text.Encoding]::UTF8)
        try {
            $raw = $reader.ReadToEnd()
        } finally {
            $reader.Close()
        }
    } finally {
        $response.Close()
    }
    $result = $raw | ConvertFrom-Json

    $result | ConvertTo-Json -Depth 100
} finally {
    if ($StopAfter) {
        try {
            & (Join-Path $ScriptDir "stop_server.ps1") -Port $Port | Out-Null
        } catch {
            Write-Warning "LocalOCR StopAfter cleanup did not complete: $($_.Exception.Message)"
        }
    }
}
