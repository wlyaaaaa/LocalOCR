# Start the WSL API with an explicit standard-handle allowlist.
param(
    [ValidateRange(1, 65535)][int]$Port = 18665,
    [ValidateSet('127.0.0.1')][string]$HostAddress = '127.0.0.1',
    [ValidateRange(1, 3600)][int]$StartupTimeoutSec = 600
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerDir = Join-Path $ScriptDir '_server'
$LogPath = Join-Path $ServerDir 'localocr-api.log'
[void][System.Net.IPAddress]::Parse($HostAddress)

function Get-LocalOcrHealthKind {
    param([AllowNull()]$Health)
    if ($null -eq $Health) { return 'unavailable' }
    $names = @($Health.PSObject.Properties.Name)
    if ($names -contains 'service') {
        if ($Health.service -ne 'localocr') { throw "Port $Port is a non-LocalOCR service." }
        if ($Health.ok -ne $true) { throw "Port $Port is LocalOCR but is not healthy." }
        return 'ready'
    }
    if ($Health.ok -eq $true -and $names -contains 'gpu' -and
        $names -contains 'loaded_engines' -and $names -contains 'loaded_models') { return 'legacy_unknown' }
    throw "Port $Port is a non-LocalOCR service."
}

function Assert-LocalOcrHealthPayload {
    param([AllowNull()]$Health)
    return Get-LocalOcrHealthKind -Health $Health
}

function Get-LocalOcrHealth {
    try {
        $health = Invoke-RestMethod -Uri "http://${HostAddress}:$Port/health" -Method Get -TimeoutSec 5
    } catch { return $null }
    $kind = Get-LocalOcrHealthKind $health
    if ($kind -eq 'legacy_unknown') {
        $health | Add-Member -NotePropertyName readiness_unknown -NotePropertyValue $true -Force
    }
    return $health
}

function Assert-LocalOcrPortBindable {
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse($HostAddress), $Port)
        $listener.Start()
    } catch {
        throw "Cannot bind ${HostAddress}:$Port before WSL startup; verify the listener and excluded port ranges. $($_.Exception.Message)"
    } finally {
        if ($listener) { $listener.Stop() }
    }
}

function Start-LocalOcrServerProcess {
    # WSL needs valid std handles, but must not inherit any caller pipe. A
    # allowlist gives it only NUL and a dedicated log, never the client's pipes.
    if (-not ('LocalOcrDetachedProcess' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public static class LocalOcrDetachedProcess {
    [StructLayout(LayoutKind.Sequential)] struct Security { public int size; public IntPtr descriptor; public int inherit; }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct Startup {
        public int size; public string reserved,desktop,title;
        public int x,y,cx,cy,xChars,yChars,fill,flags;
        public short show,reservedSize; public IntPtr reservedData,input,output,error;
    }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedStartup { public Startup startup; public IntPtr attributes; }
    [StructLayout(LayoutKind.Sequential)] struct ProcessInfo { public IntPtr process,thread; public int pid,tid; }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateFileW(string path,uint access,uint share,ref Security security,uint creation,uint flags,IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool InitializeProcThreadAttributeList(IntPtr list,int count,int flags,ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool UpdateProcThreadAttribute(IntPtr list,uint flags,IntPtr attribute,IntPtr value,IntPtr size,IntPtr previous,IntPtr returned);
    [DllImport("kernel32.dll")] static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool CreateProcessW(string app,StringBuilder command,IntPtr pa,IntPtr ta,bool inherit,uint flags,IntPtr env,string cwd,ref ExtendedStartup startup,out ProcessInfo process);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    static string Quote(string value) {
        if(value.Length > 0 && value.IndexOfAny(new char[] {' ', '\t', '\r', '\n', '"'}) < 0) return value;
        var result = new StringBuilder("\""); int backslashes=0;
        foreach(char c in value) {
            if(c=='\\') { backslashes++; continue; }
            result.Append('\\', c=='"' ? backslashes*2+1 : backslashes).Append(c); backslashes=0;
        }
        return result.Append('\\',backslashes*2).Append('"').ToString();
    }
    public static int Start(string app,string[] args,string cwd,string logPath) {
        var security=new Security(); security.size=Marshal.SizeOf(security); security.inherit=1;
        IntPtr nul=CreateFileW("NUL",0xC0000000,3,ref security,3,0,IntPtr.Zero);
        if(nul==new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
        IntPtr log=CreateFileW(logPath,4,3,ref security,4,0,IntPtr.Zero);
        if(log==new IntPtr(-1)) { CloseHandle(nul); throw new Win32Exception(Marshal.GetLastWin32Error()); }
        IntPtr attributes=IntPtr.Zero, handles=IntPtr.Zero; bool initialized=false;
        try {
            IntPtr size=IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero,1,0,ref size);
            attributes=Marshal.AllocHGlobal(size);
            if(!InitializeProcThreadAttributeList(attributes,1,0,ref size)) throw new Win32Exception(Marshal.GetLastWin32Error());
            initialized=true;
            handles=Marshal.AllocHGlobal(IntPtr.Size*2); Marshal.WriteIntPtr(handles,nul); Marshal.WriteIntPtr(handles,IntPtr.Size,log);
            if(!UpdateProcThreadAttribute(attributes,0,new IntPtr(0x00020002),handles,new IntPtr(IntPtr.Size*2),IntPtr.Zero,IntPtr.Zero))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            var startup=new ExtendedStartup(); startup.startup.size=Marshal.SizeOf(startup);
            startup.startup.flags=0x100; startup.startup.input=nul; startup.startup.output=log; startup.startup.error=log;
            startup.attributes=attributes;
            var command=new StringBuilder(Quote(app)); foreach(var arg in args) command.Append(' ').Append(Quote(arg));
            ProcessInfo process;
            if(!CreateProcessW(app,command,IntPtr.Zero,IntPtr.Zero,true,0x08080000,IntPtr.Zero,cwd,ref startup,out process))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            CloseHandle(process.thread); CloseHandle(process.process); return process.pid;
        } finally {
            if(initialized) DeleteProcThreadAttributeList(attributes);
            if(attributes!=IntPtr.Zero) Marshal.FreeHGlobal(attributes);
            if(handles!=IntPtr.Zero) Marshal.FreeHGlobal(handles);
            CloseHandle(nul);
            CloseHandle(log);
        }
    }
}
'@
    }
    $command = "cd /mnt/e/Projects/Tools/LocalOCR && exec scripts/run_in_wsl.sh -m localocr.server --host '$HostAddress' --port $Port >> '/mnt/e/Projects/Tools/LocalOCR/_server/localocr-api.log' 2>&1"
    $nativeWsl = Join-Path $env:WINDIR 'System32\wsl.exe'
    $processId = [LocalOcrDetachedProcess]::Start($nativeWsl,@('-d','Ubuntu','-e','bash','-lc',$command),$ScriptDir,(Join-Path $ServerDir 'wsl-launcher.log'))
    return "LocalOCR WSL launcher started: $processId"
}

$mutex = [System.Threading.Mutex]::new($false, "Local\LocalOCR-API-$Port")
$hasMutex = $false
try {
    $health = Get-LocalOcrHealth
    if ($health) {
        if ($health.readiness_unknown) { Write-Warning 'Legacy LocalOCR API: readiness_unknown; update the API before work.' }
        Write-Host '[LocalOCR] API already running.'
        return
    }
    try { $hasMutex = $mutex.WaitOne([TimeSpan]::FromSeconds($StartupTimeoutSec)) }
    catch [System.Threading.AbandonedMutexException] { $hasMutex = $true }
    if (-not $hasMutex) { throw 'LocalOCR startup lock timed out.' }
    $health = Get-LocalOcrHealth
    if ($health) { Write-Host '[LocalOCR] API already running.'; return }
    Assert-LocalOcrPortBindable
    New-Item -ItemType Directory -Force -Path $ServerDir | Out-Null
    Write-Host (Start-LocalOcrServerProcess)
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($StartupTimeoutSec)
    do {
        Start-Sleep -Seconds 1
        $health = Get-LocalOcrHealth
        if ($health) { Write-Host '[LocalOCR] API ready; GPU probing happens under the first work lease.'; return }
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    if (Test-Path -LiteralPath $LogPath) { Get-Content -LiteralPath $LogPath -Tail 30 | Write-Warning }
    throw 'LocalOCR API startup timed out; inspect its existing Linux PID before retrying.'
} finally {
    if ($hasMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
