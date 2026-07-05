from __future__ import annotations

import re
from pathlib import Path

_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def to_wsl_path(raw: str | Path) -> Path:
    """Normalize Windows or WSL-style paths for code running inside WSL."""
    text = str(raw).strip().strip('"')
    match = _WIN_DRIVE_RE.match(text)
    if match:
        drive = match.group(1).lower()
        rest = match.group(2).replace("\\", "/")
        return Path(f"/mnt/{drive}/{rest}")
    return Path(text).expanduser()


def to_windows_hint(path: str | Path) -> str:
    """Return a friendly Windows path hint for /mnt/<drive>/... paths."""
    text = str(path)
    match = re.match(r"^/mnt/([a-zA-Z])/(.*)$", text)
    if not match:
        return text
    drive = match.group(1).upper()
    rest = match.group(2).replace("/", "\\")
    return f"{drive}:\\{rest}"
