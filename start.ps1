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
foreach ($a in $InputArgs) {
    if ($a -match '^--') {
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
    Write-Host "[结束] 退出码 $code。输出目录: $ScriptDir\outputs" -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "[完成] 输出目录: $ScriptDir\outputs (每个文件产出 .txt/.md/.json 三份)" -ForegroundColor Green
}
exit $code
