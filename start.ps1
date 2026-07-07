# LocalOCR Windows 启动入口（PowerShell）
# 拖入文件/文件夹/PDF，或：.\start.ps1 "C:\路径\文件或文件夹" [--engine auto|ocr|vl|structure] [--model profile-id]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $Args -or $Args.Count -eq 0) {
    Write-Host "用法：把图片 / PDF / 文件夹拖到 start.bat 上，或：.\start.ps1 `"路径`" [--engine auto|ocr|vl|structure] [--model profile-id]"
    exit 1
}

# 把 Windows 路径转成 WSL 的 /mnt/e/... 形式，逐个传给 CLI。
$wslArgs = @()
foreach ($a in $Args) {
    if ($a -match '^--') {
        $wslArgs += $a
    } elseif (Test-Path -LiteralPath $a) {
        $full = (Resolve-Path -LiteralPath $a).Path
        $wslArgs += ($full -replace '^([A-Za-z]):', { '/mnt/' + $args[0].Groups[1].Value.ToLower() } -replace '\\', '/')
    } else {
        $wslArgs += $a
    }
}

$argStr = ($wslArgs | ForEach-Object { '"' + $_ + '"' }) -join ' '
$cmd = "bash /mnt/e/LocalOCR/scripts/run_in_wsl.sh -m localocr.cli $argStr"
Write-Host "[LocalOCR] 启动识别..." -ForegroundColor Cyan
wsl -d Ubuntu -e bash -c $cmd
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ""
    Write-Host "[结束] 退出码 $code。输出目录: $ScriptDir\outputs" -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "[完成] 输出目录: $ScriptDir\outputs (每个文件产出 .txt/.md/.json 三份)" -ForegroundColor Green
}
exit $code
