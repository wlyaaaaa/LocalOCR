@echo off
REM LocalOCR 启动入口（拖拽友好）：把图片/PDF/文件夹拖到本文件上松手即可。
REM 或命令行：start.bat "C:\路径\文件或文件夹" --engine auto --model ppocrv6-medium
setlocal
set "SCRIPT_DIR=%~dp0"
if "%~1"=="" (
    echo 用法：把图片 / PDF / 文件夹拖到本文件上，或：start.bat "路径" --engine auto --model ppocrv6-medium
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start.ps1" %*
endlocal
