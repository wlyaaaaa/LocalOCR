"""Host physical memory, including the Windows host when running inside WSL."""

import json
import os
import platform
import subprocess
import sys

import psutil


def host_memory_bytes(*, timeout: float = 8) -> tuple[int, int]:
    is_wsl = sys.platform.startswith("linux") and (
        os.environ.get("WSL_INTEROP") or "microsoft" in platform.release().lower()
    )
    if is_wsl:
        script = (
            "$m=Get-CimInstance Win32_OperatingSystem -ErrorAction Stop; "
            "@{total=[long]$m.TotalVisibleMemorySize; "
            "available=[long]$m.FreePhysicalMemory}|ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, check=True, timeout=min(8, max(0.01, timeout)),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        data = json.loads(completed.stdout)
        total, available = int(data["total"]) * 1024, int(data["available"]) * 1024
    else:
        memory = psutil.virtual_memory()
        total, available = int(memory.total), int(memory.available)
    if total <= 0 or not 0 <= available <= total:
        raise ValueError("Invalid host physical memory counters")
    return total, available
