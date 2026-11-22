"""A suspension must be reported as a suspension, not as an unproven write.

X answers a write from a suspended account with HTTP 200 and an errors array.
A DOM-only check cannot tell that apart from "the write landed and the page has
not repainted", so the driver keeps spending actions on an account that cannot
accept any. These codes are the difference.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT / "src"), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from account_state import classify_refusal, is_suspended, scan_state  # noqa: E402


class ClassifyRefusalTest(unittest.TestCase):
    def test_the_like_refusal_names_a_suspension(self):
        rejection = {
            "code": 141,
            "message": "Authorization: User (uid: 1) is suspended, deactivated or offboarded",
        }
        self.assertEqual(classify_refusal(rejection), "suspended")
        self.assertTrue(is_suspended(rejection))

    def test_the_repost_refusal_names_a_suspension_without_the_word(self):
        rejection = {
            "code": 37,
            "message": "Authorization: Denied by access control: Missing TwitterUserNotSuspended",
        }
        self.assertEqual(classify_refusal(rejection), "suspended")

    def test_a_scarecrow_block_is_policy_not_suspension(self):
        rejection = {"code": 283,
                     "message": "Authorization: User 1 favoriting tweet 2 was denied by scarecrow"}
        self.assertEqual(classify_refusal(rejection), "policy")
        self.assertFalse(is_suspended(rejection))

    def test_the_follow_allowance_is_its_own_class(self):
        rejection = {"code": 161,
                     "message": "You are unable to follow more people at this time."}
        self.assertEqual(classify_refusal(rejection), "allowance")

    def test_an_unsuspected_code_stays_unknown(self):
        self.assertEqual(classify_refusal({"code": 999, "message": "nope"}), "unknown")

    def test_a_string_code_is_understood(self):
        self.assertEqual(classify_refusal({"code": "37", "message": ""}), "suspended")

    def test_none_is_unknown(self):
        self.assertEqual(classify_refusal(None), "unknown")
        self.assertFalse(is_suspended(None))

    def test_the_marker_matches_case_insensitively(self):
        rejection = {"code": 0, "message": "ACCOUNT SUSPENDED"}
        self.assertEqual(classify_refusal(rejection), "suspended")


class ScanStateTest(unittest.TestCase):
    def _ledger(self, rows: list[dict]) -> dict:
        return {"actions": {("act_%d" % i): row for i, row in enumerate(rows)}}

    def _write(self, directory: Path, ledger: dict) -> None:
        (directory / "state.json").write_text(json.dumps(ledger), encoding="utf-8")

    def test_a_suspended_refusal_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._write(directory, self._ledger([{
                "action_id": "a1", "type": "repost_post",
                "status": "failed", "completed_at": "2026-11-14T16:38:55",
                "error": {"code": "X_WRITE_REJECTED", "details": {
                    "rejection_code": 37,
                    "rejection": {"code": 37, "message": "Missing TwitterUserNotSuspended"},
                }},
            }]))
            report = scan_state(directory)
            self.assertTrue(report["suspended"])
            self.assertEqual(report["suspended_refusals"], 1)
            self.assertEqual(report["codes"], ["37"])

    def test_policy_refusals_alone_do_not_mean_suspension(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._write(directory, self._ledger([{
                "action_id": "a1", "type": "like_post", "status": "failed",
                "completed_at": "2026-11-13T23:08:29",
                "error": {"code": "X_WRITE_REJECTED", "details": {
                    "rejection": {"code": 283, "message": "denied by scarecrow"},
                }},
            }]))
            report = scan_state(directory)
            self.assertFalse(report["suspended"])
            self.assertEqual(report["verdicts"], {"policy": 1})

    def test_a_missing_ledger_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = scan_state(Path(tmp))
            self.assertFalse(report["ok"])
            self.assertEqual(report["error"], "STATE_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
