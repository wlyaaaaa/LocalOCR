"""Host physical memory, including the Windows host when running inside WSL."""

import atexit
import os
import platform
import select
import subprocess
import sys
import threading
import time

import psutil

_HOST_MEMORY_PROBE_COMMAND = [
    "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
    "$ErrorActionPreference='Stop'; while ($null -ne [Console]::ReadLine()) { "
    "$m=Get-CimInstance Win32_OperatingSystem -ErrorAction Stop; "
    "[Console]::Out.WriteLine(('{0} {1}' -f [long]$m.TotalVisibleMemorySize, "
    "[long]$m.FreePhysicalMemory)); [Console]::Out.Flush() }",
]
_probe_lock = threading.Lock()
_probe_process = None


def _close_probe() -> None:
    global _probe_process
    process, _probe_process = _probe_process, None
    if process is not None:
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=1)
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


def close_host_memory_probe() -> None:
    with _probe_lock:
        _close_probe()


atexit.register(close_host_memory_probe)


def _probe_host_memory(timeout: float) -> tuple[int, int]:
    global _probe_process
    deadline = time.monotonic() + timeout
    if not _probe_lock.acquire(timeout=timeout):
        raise TimeoutError("Host memory probe is busy")
    try:
        if _probe_process is not None and _probe_process.poll() is not None:
            _close_probe()
        if _probe_process is None:
            _probe_process = subprocess.Popen(
                _HOST_MEMORY_PROBE_COMMAND, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        process = _probe_process
        process.stdin.write(b"\n")
        process.stdin.flush()
        line = b""
        while b"\n" not in line:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                raise TimeoutError("Host memory probe timed out")
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                raise EOFError("Host memory probe exited")
            line += chunk
            if len(line) > 128:
                raise ValueError("Invalid host physical memory counters")
        total, available = (int(field) * 1024 for field in line.split())
        if total <= 0 or not 0 <= available <= total:
            raise ValueError("Invalid host physical memory counters")
        return total, available
    except BaseException:
        _close_probe()
        raise
    finally:
        _probe_lock.release()


def host_memory_bytes(*, timeout: float = 8) -> tuple[int, int]:
    is_wsl = sys.platform.startswith("linux") and (
        os.environ.get("WSL_INTEROP") or "microsoft" in platform.release().lower()
    )
    if is_wsl:
        total, available = _probe_host_memory(min(8, max(0.01, timeout)))
    else:
        memory = psutil.virtual_memory()
        total, available = int(memory.total), int(memory.available)
    if total <= 0 or not 0 <= available <= total:
        raise ValueError("Invalid host physical memory counters")
    return total, available
