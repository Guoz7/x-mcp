"""Manual login must not depend on a connected automation driver."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import owner_login_open as login


class NativeLoginTests(unittest.TestCase):
    def test_launch_preserves_profile_without_remote_debugging(self):
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired("chrome", 1), 0]
        with patch.object(login, "find_chrome", return_value="chrome.exe"), \
                patch.object(login.subprocess, "Popen", return_value=process) as launch, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(login.open_native(ROOT, wait_seconds=3), 0)
        args = launch.call_args.args[0]
        self.assertIn("--user-data-dir=" + str(ROOT), args)
        self.assertEqual(args[-1], "https://x.com/home")
        self.assertFalse(any("remote-debugging" in a or "enable-automation" in a for a in args))
        self.assertIn('"authentication_verified": false', output.getvalue())
        process.terminate.assert_not_called()

    def test_early_exit_is_not_claimed_as_open_window(self):
        process = Mock()
        process.wait.return_value = 0
        with patch.object(login, "find_chrome", return_value="chrome.exe"), \
                patch.object(login.subprocess, "Popen", return_value=process), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(login.open_native(ROOT, wait_seconds=3), 3)
        self.assertIn("OWNER_BROWSER_EARLY_EXIT", output.getvalue())
        self.assertNotIn('"window": "launched"', output.getvalue())

    def test_timeout_leaves_manual_login_untouched(self):
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("chrome", 1)
        with patch.object(login, "find_chrome", return_value="chrome.exe"), \
                patch.object(login.subprocess, "Popen", return_value=process), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(login.open_native(ROOT, wait_seconds=0.01), 3)
        self.assertIn("OWNER_LOGIN_WAIT_EXPIRED", output.getvalue())
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_missing_binary_and_start_failure_are_explicit(self):
        with patch.object(login, "find_chrome", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(login.open_native(ROOT, wait_seconds=1), 2)
        with patch.object(login, "find_chrome", return_value="chrome.exe"), \
                patch.object(login.subprocess, "Popen", side_effect=OSError), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(login.open_native(ROOT, wait_seconds=1), 1)

    def test_default_login_uses_native_backend(self):
        with patch.dict(os.environ, {
            "X_STARTER_OWNER_PROFILE": str(ROOT),
            "X_BROWSER_EXECUTOR_HEADLESS": "0",
        }), patch.object(login, "open_native", return_value=0) as launch, \
                patch.object(login, "Browser") as automation:
            os.environ.pop("X_STARTER_OWNER_LOGIN_BACKEND", None)
            self.assertEqual(login.main(), 0)
        self.assertEqual(launch.call_args.args[0], ROOT)
        automation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
