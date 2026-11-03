"""Offline account-ledger gate checks; no network or browser."""

import tempfile
import unittest

from browser_bridge import BrowserBridge, BridgeError


class RateGateClockTests(unittest.TestCase):
    def _seed(self, bridge, retry=None):
        data = bridge._read()
        data["actions"]["old"] = {
            "action_id": "old", "type": "follow_profile", "status": "uncertain",
            "completed_at": "1970-01-01T00:16:40Z",
            "error": {"code": "X_RATE_LIMITED", "details": (
                {} if retry is None else {"retry_after_seconds": retry}
            )},
        }
        data["actions"]["new"] = {
            "action_id": "new", "type": "follow_profile", "status": "pending",
            "session_id": "new-session", "payload": {},
            "created_at": "1970-01-01T00:16:40Z", "instructions": {}, "result_schema": {},
        }
        bridge._write(data)

    def test_unknown_gate_survives_reopen_and_leaves_pending_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            self._seed(BrowserBridge(directory))
            bridge = BrowserBridge(directory, now=lambda: 100000)
            with self.assertRaises(BridgeError) as caught:
                bridge.next_action(session_id="new-session")
            self.assertEqual(caught.exception.code, "X_RATE_LIMITED")
            self.assertEqual(caught.exception.details, {"recovery": "manual_recovery_required"})
            self.assertEqual(bridge._read()["actions"]["new"]["status"], "pending")

    def test_numeric_gate_expires_at_recorded_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [1010]
            bridge = BrowserBridge(directory, now=lambda: clock[0])
            self._seed(bridge, 60)
            for now, remaining in ((1010, 50), (1059, 1)):
                clock[0] = now
                with self.assertRaises(BridgeError) as caught:
                    bridge.next_action(session_id="new-session")
                self.assertEqual(caught.exception.details["retry_after_seconds"], remaining)
            clock[0] = 1060
            self.assertEqual(bridge.next_action(session_id="new-session")["action_id"], "new")

    def test_invalid_retry_after_fails_closed(self):
        for retry in (True, -1, "60", float("nan")):
            with self.subTest(retry=retry), tempfile.TemporaryDirectory() as directory:
                bridge = BrowserBridge(directory, now=lambda: 100000)
                self._seed(bridge, retry)
                with self.assertRaises(BridgeError) as caught:
                    bridge.next_action(session_id="new-session")
                self.assertEqual(caught.exception.details["recovery"], "manual_recovery_required")

    def test_readonly_action_can_be_claimed_under_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, now=lambda: 1010)
            self._seed(bridge)
            data = bridge._read()
            data["actions"]["new"]["type"] = "observe_account"
            bridge._write(data)
            self.assertEqual(bridge.next_action(session_id="new-session")["action_id"], "new")

    def test_existing_claim_cannot_issue_write_grant_under_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, now=lambda: 1010)
            self._seed(bridge)
            data = bridge._read()
            data["actions"]["new"].update(status="claimed", claim_token="test-claim")
            bridge._write(data)
            with self.assertRaises(BridgeError) as caught:
                bridge.verify_claim("new", "test-claim", session_id="new-session")
            self.assertEqual(caught.exception.code, "X_RATE_LIMITED")


if __name__ == "__main__":
    unittest.main()
