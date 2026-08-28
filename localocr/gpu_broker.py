from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable


class GpuBrokerError(RuntimeError):
    pass


class GpuBrokerConflict(GpuBrokerError):
    def __init__(
        self, message: str, *, owner: str = "unknown", reason: str = "gpu_conflict"
    ):
        super().__init__(message)
        self.owner = owner
        self.reason = reason


class GpuBrokerLeaseLost(GpuBrokerError):
    pass


Transport = Callable[[str, dict], dict]


def _urllib_transport(base_url: str) -> Transport:
    def send(action: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{base_url}/_gpu_broker/{action}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8"))
            except Exception:
                raise GpuBrokerError(f"GPU broker HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise GpuBrokerError(
                f"GPU broker unavailable: {type(exc).__name__}: {exc}"
            ) from exc

    return send


def _powershell_transport(base_url: str) -> Transport:
    def send(action: str, payload: dict) -> dict:
        payload_b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode(
            "ascii"
        )
        script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$json = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload_b64}'))
try {{
  $result = Invoke-RestMethod -Uri '{base_url}/_gpu_broker/{action}' -Method Post -ContentType 'application/json; charset=utf-8' -Body $json -TimeoutSec 10
  $result | ConvertTo-Json -Depth 10 -Compress
}} catch {{
  if ($_.ErrorDetails.Message) {{ $_.ErrorDetails.Message }} else {{ throw }}
}}
"""
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            check=False,
            capture_output=True,
            timeout=20,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        if completed.returncode != 0 or not stdout:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise GpuBrokerError(
                stderr or stdout or "GPU broker PowerShell bridge failed"
            )
        try:
            return json.loads(stdout.splitlines()[-1])
        except json.JSONDecodeError as exc:
            raise GpuBrokerError(
                f"Invalid GPU broker response: {stdout[-500:]}"
            ) from exc

    return send


def default_transport(base_url: str) -> Transport:
    if os.environ.get("WSL_INTEROP") or os.path.exists(
        "/proc/sys/fs/binfmt_misc/WSLInterop"
    ):
        return _powershell_transport(base_url)
    return _urllib_transport(base_url)


def verify_inherited_gpu_lease(binding: dict | None) -> None:
    """An internal child must prove a live token before importing the GPU runtime."""
    if not isinstance(binding, dict) or not binding.get("token"):
        raise GpuBrokerError("A supervised GPU worker requires a live parent lease.")
    owner = str(binding.get("owner") or "")
    if owner not in {"localocr", "localocr-cli"}:
        raise GpuBrokerError("The inherited lease is not a LocalOCR lease.")
    transport = default_transport(str(binding["base_url"]))
    result = transport(
        "renew", {"token": binding["token"], "ttl_seconds": binding["ttl_seconds"]}
    )
    if not result.get("ok") or result.get("owner") != owner:
        raise GpuBrokerLeaseLost("The inherited GPU lease is no longer valid.")


class GpuBrokerLease:
    def __init__(
        self,
        owner: str,
        *,
        base_url: str | None = None,
        ttl_seconds: int = 90,
        renew_interval_seconds: int = 20,
        transport: Transport | None = None,
    ) -> None:
        self.owner = owner
        self.base_url = (
            base_url
            or os.environ.get("LOCAL_GPU_BROKER_URL")
            or "http://127.0.0.1:32100"
        ).rstrip("/")
        self.ttl_seconds = ttl_seconds
        self.renew_interval_seconds = renew_interval_seconds
        self.transport = transport or default_transport(self.base_url)
        self.token = ""
        self._stop = threading.Event()
        self._renew_thread: threading.Thread | None = None
        self._transport_lock = threading.Lock()
        self._lost_error: GpuBrokerLeaseLost | None = None
        self._last_renewed = 0.0

    @property
    def worker_binding(self) -> dict:
        self.raise_if_lost()
        return {
            "token": self.token,
            "owner": self.owner,
            "base_url": self.base_url,
            "ttl_seconds": self.ttl_seconds,
        }

    def raise_if_lost(self) -> None:
        if self._lost_error is not None:
            raise self._lost_error
        if self.token and time.monotonic() - self._last_renewed >= self.ttl_seconds:
            self._lost_error = GpuBrokerLeaseLost(
                "GPU lease expired without a successful renewal."
            )
            raise self._lost_error

    def __enter__(self):
        self._stop.clear()
        self._lost_error = None
        result = self.transport(
            "acquire", {"owner": self.owner, "ttl_seconds": self.ttl_seconds}
        )
        if not result.get("ok"):
            active_owner = result.get("owner") or "unknown"
            reason = result.get("reason") or "gpu_conflict"
            raise GpuBrokerConflict(
                f"GPU broker blocked {self.owner}: {reason}; active={active_owner}",
                owner=str(active_owner),
                reason=str(reason),
            )
        self.token = str(result.get("token") or "")
        if not self.token:
            raise GpuBrokerError("GPU broker returned no lease token")
        self._last_renewed = time.monotonic()
        if self.renew_interval_seconds > 0:
            self._renew_thread = threading.Thread(target=self._renew_loop, daemon=True)
            self._renew_thread.start()
        return self

    def _renew_loop(self) -> None:
        while not self._stop.wait(self.renew_interval_seconds):
            try:
                with self._transport_lock:
                    if self._stop.is_set():
                        return
                    result = self.transport(
                        "renew", {"token": self.token, "ttl_seconds": self.ttl_seconds}
                    )
                if not result.get("ok") or result.get("owner") != self.owner:
                    self._lost_error = GpuBrokerLeaseLost(
                        f"GPU lease renewal rejected: {result.get('reason') or 'unknown'}."
                    )
                    return
                self._last_renewed = time.monotonic()
            except Exception as exc:
                self._lost_error = GpuBrokerLeaseLost(
                    f"GPU lease renewal failed: {type(exc).__name__}."
                )
                return

    def __exit__(self, exc_type, _exc, _tb):
        self._stop.set()
        # Never release while an in-flight renewal can revive the token.
        with self._transport_lock:
            if self.token:
                try:
                    result = self.transport("release", {"token": self.token})
                    if (
                        not result.get("ok")
                        and exc_type is None
                        and self._lost_error is None
                    ):
                        raise GpuBrokerError("GPU lease release was rejected.")
                except Exception:
                    if exc_type is None:
                        raise
                finally:
                    self.token = ""
        if self._renew_thread:
            self._renew_thread.join(timeout=1)
