"""Synthetic result-contract checks; no browser, service or real action."""
import copy
import hashlib
import json
import unittest

from visual_reconciliation import validate_visual_run

PNG = b"\x89PNG\r\n\x1a\nfixture"
DIGEST = hashlib.sha256(PNG).hexdigest()


def fixture():
    tools = ["browser_open", "browser_observe", "browser_screenshot", "browser_close"]
    rows = []
    for i, tool in enumerate(tools):
        rows.append({"type": "tool_call", "callId": str(i), "tool": "mcp_xstarter_" + tool,
                     "input": {"include_data": True} if tool == "browser_screenshot" else {}})
        rows.append({"type": "tool_result", "callId": str(i), "status": "completed",
                     "result": json.dumps({"data": {"sha256": DIGEST}})})
    rows.append({"type": "final", "text": json.dumps({
        "target_ref": "abc", "visible_state": "Follow", "pixels_available": True,
        "screenshot_sha256": DIGEST, "security_notice": None,
        "reason": "The profile button reads Follow.",
    })})
    return rows


class VisualReconciliationTests(unittest.TestCase):
    def check(self, rows):
        return validate_visual_run(rows, expected_ref="abc", attachment_bytes=PNG)

    def test_valid_observation_never_authorizes_retry_or_promotion(self):
        report = self.check(fixture())
        self.assertTrue(report["valid"])
        self.assertFalse(report["retry_authorized"])
        self.assertFalse(report["promote_historical_action"])

    def test_invented_eligibility_prose_is_rejected(self):
        rows = fixture()
        rows[-1]["text"] = "I require Follows you evidence before acting."
        self.assertIn("final_not_json_object", self.check(rows)["errors"])

    def test_image_refusal_and_wrong_image_are_rejected(self):
        rows = fixture()
        rows[5]["result"] += "\n[image unavailable: image/png]"
        self.assertIn("image_admission_refused", self.check(rows)["errors"])
        rows[5]["result"] = json.dumps({"data": {"sha256": "0" * 64}})
        self.assertIn("screenshot_attachment_mismatch", self.check(rows)["errors"])

    def test_write_tool_is_rejected(self):
        rows = fixture()
        rows[0]["tool"] = "mcp_xstarter_browser_click"
        self.assertIn("non_read_only_tool_called", self.check(rows)["errors"])

    def test_duplicate_tool_results_are_rejected(self):
        rows = fixture()
        rows.insert(-1, copy.deepcopy(rows[5]))
        self.assertIn("tool_call_result_identity_mismatch", self.check(rows)["errors"])

    def test_security_notice_cannot_prove_following(self):
        rows = fixture()
        final = json.loads(rows[-1]["text"])
        final.update(security_notice=True, visible_state="Following")
        rows[-1]["text"] = json.dumps(final)
        self.assertIn("security_interstitial_state_not_unknown", self.check(rows)["errors"])

    def test_truncation_is_preserved_not_silently_zeroed(self):
        rows = fixture()
        rows[3]["truncated"] = True
        report = self.check(rows)
        self.assertEqual(report["truncated_tool_results"], 1)

    def test_mismatched_model_evidence_is_rejected(self):
        for key, value, error in (("target_ref", "other", "target_ref_mismatch"),
                                  ("pixels_available", False, "pixels_not_observed"),
                                  ("screenshot_sha256", "0" * 64, "model_image_mismatch"),
                                  ("visible_state", "Eligible", "invalid_visible_state")):
            with self.subTest(key=key):
                rows = copy.deepcopy(fixture())
                final = json.loads(rows[-1]["text"])
                final[key] = value
                rows[-1]["text"] = json.dumps(final)
                self.assertIn(error, self.check(rows)["errors"])
