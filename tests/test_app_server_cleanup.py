"""Tests that each App Server startup failure stage tears down the spawned
process and closes its pipes (no leaked child process or open handles).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_team.app_server import AppServerRuntime, AppServerStartupError

FAKE_SERVER = Path(__file__).with_name("fake_app_server.py")


class AppServerCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.spawned: list[subprocess.Popen] = []
        self.runtime = AppServerRuntime(
            [sys.executable, str(FAKE_SERVER)],
            startup_timeout=2.0,
            stop_timeout=2.0,
            stderr_log_path=self.base / "app-server.stderr.log",
        )
        real_popen = subprocess.Popen

        def tracking_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            self.spawned.append(process)
            return process

        self._popen_patch = patch(
            "codex_team.app_server.subprocess.Popen", side_effect=tracking_popen
        )
        self._popen_patch.start()

    def tearDown(self) -> None:
        self._popen_patch.stop()
        for process in self.spawned:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.temporary.cleanup()

    def _assert_cleaned_up(self) -> None:
        self.assertTrue(self.spawned, "expected a child process to be spawned")
        process = self.spawned[-1]
        process.wait(timeout=5)
        self.assertIsNotNone(process.poll(), "child process was not terminated")
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                self.assertTrue(stream.closed, "pipe was left open")

    def test_job_object_failure_cleans_up(self) -> None:
        with patch(
            "codex_team.app_server.ProcessJob.attach",
            side_effect=RuntimeError("job attach failed"),
        ):
            with self.assertRaisesRegex(AppServerStartupError, "Job Object"):
                self.runtime.start()
        self._assert_cleaned_up()

    def test_identity_failure_cleans_up(self) -> None:
        with patch(
            "codex_team.app_server.get_process_identity", return_value={}
        ):
            with self.assertRaisesRegex(AppServerStartupError, "identity"):
                self.runtime.start()
        self._assert_cleaned_up()

    def test_launch_failure_raises_without_child(self) -> None:
        with patch(
            "codex_team.app_server.subprocess.Popen",
            side_effect=OSError("cannot launch"),
        ):
            with self.assertRaisesRegex(AppServerStartupError, "failed to launch"):
                self.runtime.start()
        self.assertEqual(self.spawned, [])


if __name__ == "__main__":
    unittest.main()