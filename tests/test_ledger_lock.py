"""Cross-process ledger checks using isolated files and no external service."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from browser_bridge import BrowserBridge


class LedgerLockTests(unittest.TestCase):
    def test_nested_access_is_reentrant(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory)
            with bridge._lock:
                with bridge._lock:
                    self.assertEqual(bridge.snapshot()["action_count"], 0)
            with bridge._lock:
                self.assertEqual(bridge._lock.depth, 1)

    def test_other_process_waits_for_ledger_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory)
            marker = Path(directory) / "entered"
            code = ("from browser_bridge import BrowserBridge; from pathlib import Path; "
                    f"b=BrowserBridge({directory!r}); "
                    f"Path({str(marker)!r}).write_text('entered')")
            process = None
            try:
                with bridge._lock:
                    process = subprocess.Popen(
                        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1]
                    )
                    with self.assertRaises(subprocess.TimeoutExpired):
                        process.wait(timeout=0.5)
                    self.assertFalse(marker.exists())
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertTrue(marker.exists())
            finally:
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    unittest.main()
