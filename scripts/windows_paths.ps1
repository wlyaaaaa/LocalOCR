function Invoke-LocalOcrWslPathCommand {
    param([string[]]$Arguments, [int]$TimeoutSec = 60)
    $psi = [System.Diagnostics.ProcessStartInfo]::new('wsl.exe')
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $psi.RedirectStandardOutput = $psi.RedirectStandardError = $true
    $psi.Environment['WSL_UTF8'] = '1'
    $psi.StandardOutputEncoding = $psi.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
    foreach ($argument in $Arguments) { [void]$psi.ArgumentList.Add($argument) }
    $process = [System.Diagnostics.Process]::Start($psi)
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $remaining = { [int][Math]::Max(0, $TimeoutSec * 1000 - $watch.ElapsedMilliseconds) }
    $process.StandardInput.Close()
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    try {
        $completed = $process.WaitForExit((& $remaining)) -and $stdout.Wait((& $remaining)) -and $stderr.Wait((& $remaining))
        if (-not $completed -and -not $process.HasExited) { $process.Kill() }
        if (-not $completed) { [void][System.Threading.Tasks.Task]::WaitAll(@($stdout, $stderr), 1000) }
        return [pscustomobject]@{ exit_code = if ($process.HasExited) { $process.ExitCode } else { $null }; timed_out = -not $completed
            stdout = if ($stdout.IsCompletedSuccessfully) { $stdout.Result } else { '' }
            stderr = if ($stderr.IsCompletedSuccessfully) { $stderr.Result } else { '' } }
    } finally { $process.StandardOutput.Close(); $process.StandardError.Close(); $process.Dispose() }
}

# Resolve paths through Ubuntu; three bounded attempts with 1s total backoff.
function ConvertTo-LocalOcrWslPath {
    param([Parameter(Mandatory)][string]$Path)

    if ($Path.StartsWith('/')) { return $Path }
    $fullPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    $failures = @()
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            $result = Invoke-LocalOcrWslPathCommand -Arguments @('-d', 'Ubuntu', '-e', 'wslpath', '-a', '-u', $fullPath)
            if ($result.timed_out -or $result.exit_code -ne 0 -or [string]::IsNullOrWhiteSpace($result.stdout)) {
                throw "wsl.exe exit=$($result.exit_code), timed_out=$($result.timed_out); stdout=[$($result.stdout)]; stderr=[$($result.stderr)]"
            }
            if ($failures.Count) {
                try {
                    $log = Join-Path (Split-Path -Parent $PSScriptRoot) '_server/wslpath-retries.log'
                    [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $log))
                    Add-Content -LiteralPath $log -Encoding utf8 -ErrorAction Stop -Value ("$([DateTimeOffset]::Now.ToString('o')) recovered on attempt $attempt; " + (ConvertTo-Json -InputObject $failures -Compress))
                } catch { } # Diagnostics must not turn a resolved path into failure.
            }
            return $result.stdout.TrimEnd("`r", "`n")
        } catch { $failures += "attempt ${attempt}: $($_.Exception.Message)" }
        if ($attempt -lt 3) { Start-Sleep -Milliseconds 500 }
    }
    throw "Cannot resolve the WSL path: $fullPath. $($failures -join ' | ')"
}
