# Resolve paths through the selected distribution, including non-default mounts.
function ConvertTo-LocalOcrWslPath {
    param([Parameter(Mandatory)][string]$Path)

    $fullPath = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath
    $converted = & wsl.exe -d Ubuntu -e wslpath -a -u $fullPath
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($converted)) {
        throw "Cannot resolve the WSL path: $fullPath"
    }
    return ($converted -join "`n").TrimEnd("`r", "`n")
}
