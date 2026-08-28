# Release LocalOCR GPU/API resources before starting other heavy local workloads.
param(
    [int]$Port = 18665,
    [int]$WslTimeoutSec = 10
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

try {
    & (Join-Path $ScriptDir "stop_server.ps1") -Port $Port -WslTimeoutSec $WslTimeoutSec
} catch {
    throw "[LocalOCR] Resource release failed; resources were not confirmed released: $($_.Exception.Message)"
}
Write-Host "[LocalOCR] LocalOCR resources released." -ForegroundColor Green
