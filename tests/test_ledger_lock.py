"""Cross-process ledger checks using isolated files and no external service."""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from browser_bridge import BrowserBridge
from browser_bridge import BridgeError


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

    def test_bridge_maps_lock_timeout_to_structured_error(self):
        with tempfile.TemporaryDirectory() as directory:
            from ledger_lock import LedgerLock
            first = LedgerLock(Path(directory) / ".lock", timeout_seconds=1)
            second = LedgerLock(
                Path(directory) / ".lock", timeout_seconds=0,
                on_timeout=lambda: BridgeError("BRIDGE_BUSY", "busy", details={"retryable": True}),
            )
            first.__enter__()
            try:
                with self.assertRaises((BridgeError, TimeoutError)):
                    with second:
                        pass
            finally:
                first.__exit__()


if __name__ == "__main__":
    unittest.main()
