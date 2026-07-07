from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from subprocess import TimeoutExpired

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from localocr.service import run_isolated_command


class IsolatedProcessTest(unittest.TestCase):
    def test_service_treats_structure_as_isolated_heavy_engine(self) -> None:
        service_source = (Path(__file__).resolve().parent.parent / "localocr" / "service.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("HEAVY_ISOLATED_ENGINES", service_source)
        self.assertIn('"vl"', service_source)
        self.assertIn('"structure"', service_source)
        self.assertIn("profile.engine in HEAVY_ISOLATED_ENGINES", service_source)

    def test_timeout_kills_child_process_group(self) -> None:
        if os.name != "posix":
            self.skipTest("process-group cleanup is verified in WSL/Linux")

        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            parent_code = (
                "import pathlib, subprocess, sys, time; "
                f"pid_file = pathlib.Path({str(pid_file)!r}); "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "pid_file.write_text(str(child.pid), encoding='utf-8'); "
                "time.sleep(60)"
            )

            with self.assertRaises(TimeoutExpired):
                run_isolated_command(
                    [sys.executable, "-c", parent_code],
                    cwd=Path(tmp),
                    timeout_sec=1,
                )

            deadline = time.time() + 5
            while not pid_file.exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), "test child process did not start")

            child_pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.05)

            self.fail(f"child process {child_pid} survived isolated-command timeout")


if __name__ == "__main__":
    unittest.main()
