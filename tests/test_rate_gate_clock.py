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

    def test_operator_calibration_permit_allows_one_gate_check(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, now=lambda: 1010, account_binding="owner")
            self._seed(bridge)
            data = bridge._read()
            data["account_binding"] = "owner"
            data["actions"]["new"]["payload"]["handle"] = "new"
            data["operator_recovery"] = {
                "allow_one_calibration": True,
                "account_binding": "owner",
                "target_handle": "new",
                "expires_epoch": 1100,
            }
            bridge._write(data)
            self.assertEqual(bridge.next_action(session_id="new-session")["action_id"], "new")
            self.assertTrue(bridge._read()["operator_recovery"]["consumed"])

    def _bounded(self, directory, **overrides):
        bridge = BrowserBridge(directory, now=lambda: 1010, account_binding="owner")
        self._seed(bridge)
        data = bridge._read()
        data["account_binding"] = "owner"
        data["actions"]["new"]["payload"]["handle"] = "new"
        window = {
            "mode": "bounded_follow_run",
            "account_binding": "owner",
            "allow_one_calibration": False,
            "authorized_epoch": 1000,
            "expires_epoch": 1100,
            "max_actions": 2,
            "used_actions": 0,
        }
        window.update(overrides)
        data["operator_recovery"] = window
        bridge._write(data)
        return bridge

    def test_bounded_window_allows_claim_and_counts_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self._bounded(directory)
            self.assertEqual(bridge.next_action(session_id="new-session")["action_id"], "new")
            self.assertEqual(bridge._read()["operator_recovery"]["used_actions"], 1)

    def test_bounded_window_stops_at_expiry_or_limit(self):
        cases = (
            {"expires_epoch": 1005},
            {"used_actions": 2},
            {"max_actions": 0},
            {"revoked_reason": "fresh_rate_limit"},
            {"account_binding": "other"},
        )
        for overrides in cases:
            with self.subTest(**overrides), tempfile.TemporaryDirectory() as directory:
                bridge = self._bounded(directory, **overrides)
                with self.assertRaises(BridgeError):
                    bridge.next_action(session_id="new-session")
                self.assertEqual(bridge._read()["actions"]["new"]["status"], "pending")

    def test_new_rate_limit_after_window_start_revokes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self._bounded(directory)
            data = bridge._read()
            data["actions"]["fresh"] = {
                "action_id": "fresh", "type": "follow_profile", "status": "uncertain",
                "completed_at": "1970-01-01T00:16:55Z",
                "error": {"code": "X_RATE_LIMITED", "details": {}},
            }
            bridge._write(data)
            with self.assertRaises(BridgeError):
                bridge.next_action(session_id="new-session")

    def test_bounded_window_older_rate_does_not_revoke(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self._bounded(directory)
            self.assertEqual(bridge.next_action(session_id="new-session")["action_id"], "new")

    def test_bounded_window_only_covers_follows(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = self._bounded(directory)
            data = bridge._read()
            data["actions"]["new"]["type"] = "like_post"
            bridge._write(data)
            with self.assertRaises(BridgeError):
                bridge.next_action(session_id="new-session")

    def test_conflicting_rate_headers_use_longer_window(self):
        for reset, expected in ((1050, 59), (2000, 999)):
            with self.subTest(reset=reset), tempfile.TemporaryDirectory() as directory:
                bridge = BrowserBridge(directory, now=lambda: 1001)
                self._seed(bridge, 60)
                data = bridge._read()
                data["actions"]["old"]["error"]["details"]["rate_limit"] = {"reset_epoch": reset}
                bridge._write(data)
                with self.assertRaises(BridgeError) as caught:
                    bridge.next_action(session_id="new-session")
                self.assertEqual(caught.exception.details["retry_after_seconds"], expected)

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
