#requires -Version 7.3
# LocalOCR Windows 启动入口（PowerShell）
# 拖入文件/文件夹/PDF，或：.\start.ps1 "C:\路径\文件或文件夹" [--engine auto|ocr|vl|structure] [--model profile-id]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InputArgs
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $PSScriptRoot 'scripts/windows_paths.ps1')

if (-not $InputArgs -or $InputArgs.Count -eq 0) {
    Write-Host "用法：把图片 / PDF / 文件夹拖到 start.bat 上，或：.\start.ps1 `"路径`" [--engine auto|ocr|vl|structure] [--model profile-id]"
    exit 1
}

# 把 Windows 路径转成所选 WSL 发行版中的路径，逐个传给 CLI。
$wslArgs = @()
$pathOptions = @('--out-dir', '--tmp-dir')
$valueOptions = @('--engine', '--model', '--device', '--timeout-sec')
$pendingOption = $null
$parseOptions = $true
$outputDirectory = "$ScriptDir\outputs"
foreach ($a in $InputArgs) {
    $looksLikeOption = $parseOptions -and $a.StartsWith('-') -and
        $a -ne '-' -and $a -notmatch '^-(?:\d+|\d*\.\d+)$'
    if ($pendingOption -and $looksLikeOption) { $pendingOption = $null }
    if ($pendingOption) {
        if ($pendingOption -eq '--out-dir') { $outputDirectory = if ($a.StartsWith('/')) { $a } else { $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($a) } }
        $value = if ($pendingOption -in $pathOptions) { ConvertTo-LocalOcrWslPath -Path $a } else { $a }
        $wslArgs += $value
        $pendingOption = $null
    } elseif ($parseOptions -and $a -eq '--') {
        $wslArgs += $a
        $parseOptions = $false
    } elseif ($parseOptions -and $a -match '^(--[^=]+)=(.*)$') {
        $option = $Matches[1]
        $value = $Matches[2]
        if ($option -eq '--out-dir') { $outputDirectory = if ($value.StartsWith('/')) { $value } else { $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($value) } }
        if ($option -in $pathOptions) { $value = ConvertTo-LocalOcrWslPath -Path $value }
        $wslArgs += "$option=$value"
    } elseif ($looksLikeOption) {
        $wslArgs += $a
        if ($a -in ($pathOptions + $valueOptions)) { $pendingOption = $a }
    } elseif ($a.StartsWith('/')) {
        $wslArgs += $a
    } elseif (Test-Path -LiteralPath $a) {
        $wslArgs += ConvertTo-LocalOcrWslPath -Path $a
    } else {
        $wslArgs += $a
    }
}

$runInWsl = (ConvertTo-LocalOcrWslPath -Path $PSScriptRoot) + '/scripts/run_in_wsl.sh'
Write-Host "[LocalOCR] 启动识别..." -ForegroundColor Cyan
& wsl.exe -d Ubuntu -e bash $runInWsl -m localocr.cli @wslArgs
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ""
    Write-Host "[结束] 退出码 $code。输出目录: $outputDirectory" -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "[完成] 输出目录: $outputDirectory (每个文件产出 .txt/.md/.json 三份)" -ForegroundColor Green
}
exit $code
