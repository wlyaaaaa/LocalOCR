import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from localocr import gpu_broker as broker, memory_budget as memory
from localocr.difficulty import page_structure_signals


class CurlBridgeTests(unittest.TestCase):
    def tearDown(self):
        broker._windows_curl.cache_clear()

    def test_json_status_and_stdin(self):
        for status, body in ((200, {"ok": True}), (409, {"ok": False, "owner": "renderer"})):
            with self.subTest(status=status), patch.object(broker.subprocess, "run") as run:
                run.return_value = SimpleNamespace(returncode=0, stdout=(json.dumps(body) + f"\n{status}").encode(), stderr=b"")
                payload = {"owner": "private test owner"}
                self.assertEqual(broker._curl_transport("http://localhost", "curl.exe")("acquire", payload), body)
                args, kwargs = run.call_args.args[0], run.call_args.kwargs
                self.assertEqual(json.loads(kwargs["input"]), payload)
                self.assertNotIn("private test owner", " ".join(args))
                self.assertEqual(args[args.index("--data-binary") + 1], "@-")
                self.assertEqual(kwargs["timeout"], 20)
                self.assertIn("\n%{http_code}", args)

    def test_failures(self):
        for code, stdout in ((7, b"\n000"), (0, b"not JSON\n500")):
            with self.subTest(code=code), patch.object(broker.subprocess, "run", return_value=SimpleNamespace(returncode=code, stdout=stdout, stderr=b"unreachable")):
                with self.assertRaises(broker.GpuBrokerError):
                    broker._curl_transport("http://localhost", "curl.exe")("acquire", {})

    def test_wsl_prefers_cached_curl_and_falls_back(self):
        for found, exists, expected in (("found.exe", False, "found.exe"), (None, True, "/mnt/c/Windows/System32/curl.exe"), (None, False, None)):
            broker._windows_curl.cache_clear()
            with self.subTest(expected=expected), patch.dict(os.environ, {"WSL_INTEROP": "test"}), patch.object(broker.shutil, "which", return_value=found) as which, patch.object(broker.os.path, "isfile", return_value=exists), patch.object(broker, "_curl_transport", return_value="curl") as curl, patch.object(broker, "_powershell_transport", return_value="powershell") as powershell:
                self.assertEqual(broker.default_transport("url"), "curl" if expected else "powershell")
                broker.default_transport("url")
                which.assert_called_once_with("curl.exe")
                if expected:
                    curl.assert_called_with("url", expected)
                    powershell.assert_not_called()
                else:
                    powershell.assert_called_with("url")
        with patch.dict(os.environ, {}, clear=True), patch.object(broker.os.path, "exists", return_value=False), patch.object(broker, "_urllib_transport", return_value="urllib"):
            self.assertEqual(broker.default_transport("url"), "urllib")


@unittest.skipUnless(os.name == "posix", "select on process pipes requires POSIX/WSL")
class MemoryProbeTests(unittest.TestCase):
    def tearDown(self):
        memory.close_host_memory_probe()

    def command(self, script):
        return patch.object(memory, "_HOST_MEMORY_PROBE_COMMAND", [sys.executable, "-u", "-c", script])

    def test_reuses_process_and_converts_kib(self):
        spawn = memory.subprocess.Popen
        with self.command("import sys\nfor line in sys.stdin: print('64000000 8000000', flush=True)"), patch.object(memory.subprocess, "Popen", wraps=spawn) as start:
            for _ in range(3):
                self.assertEqual(memory._probe_host_memory(2), (64000000 * 1024, 8000000 * 1024))
            start.assert_called_once()

    def test_timeout_eof_and_bad_format_kill_and_clear(self):
        for script, error in (("import sys,time; sys.stdin.readline(); time.sleep(5)", TimeoutError), ("import sys; sys.stdin.readline()", EOFError), ("import sys; sys.stdin.readline(); print('bad counters', flush=True)", ValueError)):
            spawn = memory.subprocess.Popen
            processes = []
            def start(*args, **kwargs):
                process = spawn(*args, **kwargs)
                processes.append(process)
                return process
            with self.subTest(error=error), self.command(script), patch.object(memory.subprocess, "Popen", side_effect=start):
                with self.assertRaises(error):
                    memory._probe_host_memory(0.2 if error is TimeoutError else 2)
                self.assertIsNone(memory._probe_process)
                self.assertIsNotNone(processes[0].poll())

    def test_exited_process_is_reopened(self):
        spawn = memory.subprocess.Popen
        with self.command("import sys; sys.stdin.readline(); print('64 8', flush=True)"), patch.object(memory.subprocess, "Popen", wraps=spawn) as start:
            self.assertEqual(memory._probe_host_memory(2), (65536, 8192))
            memory._probe_process.wait(timeout=2)
            self.assertEqual(memory._probe_host_memory(2), (65536, 8192))
            self.assertEqual(start.call_count, 2)


class ThinLineTests(unittest.TestCase):
    def test_thin_grid_and_dark_bands(self):
        import cv2
        import numpy as np
        table = np.full((400, 600), 255, dtype=np.uint8)
        for x in (40, 180, 320, 460):
            table[40:360, x:x + 2] = 0
        for y in (40, 140, 240, 340):
            table[y:y + 2, 40:462] = 0
        bands = np.zeros_like(table)
        bands[120:130, :] = 255
        bands[260:270, :] = 255
        bands[:, 180:190] = 255
        bands[:, 380:390] = 255
        with tempfile.TemporaryDirectory() as tmp:
            for name, image, expected in (("grid", table, True), ("bands", bands, False)):
                path = Path(tmp) / f"{name}.png"
                self.assertTrue(cv2.imwrite(str(path), image))
                self.assertEqual("ruled_table_content" in page_structure_signals({}, path), expected)

    def test_project_samples(self):
        for name, expected in (("sample_chat_screenshot.png", False), ("sample_table.png", True)):
            with self.subTest(name=name):
                self.assertEqual("ruled_table_content" in page_structure_signals({}, Path(__file__).parent / "samples" / name), expected)
