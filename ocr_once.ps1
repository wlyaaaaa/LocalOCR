# Call the LocalOCR local API once. Starts the server if needed.
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Path,

    [ValidateSet("auto", "ocr", "vl")]
    [string]$Engine = "auto",

    [switch]$Recursive,
    [string]$OutDir,
    [int]$Port = 8765,
    [string]$HostAddress = "127.0.0.1"
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

if (-not (Test-LocalOcrApi)) {
    & (Join-Path $ScriptDir "start_server.ps1") -Port $Port -HostAddress $HostAddress
}

$body = @{
    path = $Path
    engine = $Engine
    recursive = [bool]$Recursive
    write_outputs = $true
}
if ($OutDir) {
    $body.out_dir = $OutDir
}

$json = $body | ConvertTo-Json -Depth 8
$client = New-Object System.Net.WebClient
$client.Encoding = [System.Text.Encoding]::UTF8
$client.Headers.Add("Content-Type", "application/json; charset=utf-8")
$raw = $client.UploadString("$base/ocr/path", "POST", $json)
$result = $raw | ConvertFrom-Json

$result | ConvertTo-Json -Depth 100
