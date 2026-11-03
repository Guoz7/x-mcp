"""Synthetic-ledger tests for the read-only follow audit."""

import hashlib
import json
import unittest

from follow_run_audit import audit_follow_run


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def append(data, kind, subject, detail):
    prior = data["evidence"][-1]["hash"] if data["evidence"] else "0" * 64
    sequence = data["next_sequence"]
    entry = {
        "sequence": sequence,
        "at": "2026-11-03T04:00:00Z",
        "type": kind,
        "subject_id": subject,
        "detail": json.loads(json.dumps(detail)),
        "prev_hash": prior,
    }
    entry["hash"] = digest(entry)
    data["evidence"].append(entry)
    data["next_sequence"] = sequence + 1


def follow(action_id, handle, *, status="completed", changed=True,
           verified=True, state="following", claimed="2026-11-03T04:00:00Z",
           completed="2026-11-03T04:00:03Z", elapsed=None,
           error=None, execution=None):
    result = None
    if status == "completed":
        result = {
            "handle": handle,
            "profile_url": f"https://x.com/{handle}",
            "verified": verified,
            "changed": changed,
            "state": state,
            "locator_strategy": "dom",
        }
        if elapsed is not None:
            result["elapsed_seconds"] = elapsed
    return {
        "action_id": action_id,
        "type": "follow_profile",
        "status": status,
        "payload": {
            "handle": handle,
            "profile_url": f"https://x.com/{handle}",
            "claim_token": "secret-claim",
        },
        "claimed_at": claimed,
        "completed_at": completed,
        "result": result,
        "error": error,
        "execution": execution,
        "session_id": "secret-session",
    }


def ledger(actions):
    data = {
        "schema": "x-starter-browser-bridge/v1",
        "next_sequence": 1,
        "actions": {item["action_id"]: item for item in actions},
        "evidence": [],
    }
    for item in actions:
        event = {
            "completed": "action_completed",
            "uncertain": "action_uncertain",
            "failed": "action_failed",
        }.get(item["status"])
        if event:
            append(data, event, item["action_id"], {
                "type": item["type"],
                "result": item["result"],
                "error": item["error"],
                "execution": item["execution"],
            })
    return data


class FollowRunAuditTests(unittest.TestCase):
    def test_counts_unique_strict_success_and_invalid_states(self):
        data = ledger([
            follow("a1", "Alice", elapsed=2.5),
            follow("a2", "alice", elapsed=3),
            follow("a3", "Bob", changed=False),
            follow("a4", "Carol", verified=False),
            follow("a5", "Dave", state="pending"),
            follow("a6", "Eve", status="uncertain",
                   error={"code": "FOLLOW_PENDING", "message": "fixture", "details": {}}),
        ])
        report = audit_follow_run(data)
        self.assertEqual(report["follow"]["strict_verified_changed_following_unique"], 1)
        self.assertEqual(report["follow"]["duplicate_verified_targets"], 1)
        self.assertEqual(report["status_counts"], {"completed": 5, "uncertain": 1})
        self.assertEqual(report["error_counts"], {"FOLLOW_PENDING": 1})
        self.assertTrue(report["evidence"]["chain"]["valid"])
        self.assertEqual(
            report["evidence"]["comparison"]["result_evidence_mismatches"], 0
        )

    def test_tampered_evidence_is_reported_without_promotion(self):
        data = ledger([follow("a1", "Alice", elapsed=2.5)])
        data["evidence"][0]["detail"]["result"]["changed"] = False
        report = audit_follow_run(data)
        self.assertFalse(report["evidence"]["chain"]["valid"])
        self.assertFalse(report["evidence"]["comparison"]["available"])
        self.assertEqual(
            report["follow"]["strict_verified_changed_following_unique"], 1
        )
        self.assertEqual(report["follow"]["visual_fallback_success_proven"], 0)

    def test_missing_browser_timing_uses_ledger_delta_or_unknown(self):
        data = ledger([
            follow("a1", "Alice"),
            follow("a2", "Bob", status="claimed", completed=None,
                   claimed="2026-11-03T04:00:00Z"),
            follow("a3", "Carol", status="uncertain", completed=None,
                   claimed="2026-11-03T04:00:00Z",
                   error={"code": "ACTION_UNCERTAIN", "message": "fixture",
                          "details": {"attempt_elapsed_seconds": 4.25}}),
        ])
        report = audit_follow_run(data)
        rows = {row["action_ref"]: row for row in report["rows"]}
        self.assertTrue(all(row["timing"]["source"] != "browser" for row in rows.values()))
        a1 = next(row for row in report["rows"] if row["status"] == "completed")
        self.assertEqual(a1["timing"]["source"], "ledger_timestamp_elapsed")
        self.assertEqual(a1["timing"]["seconds"], 3.0)
        unresolved = next(row for row in report["rows"] if row["status"] == "claimed")
        self.assertTrue(unresolved["timing"]["unknown"])
        self.assertEqual(unresolved["timing"]["reason"], "claimed_unresolved")
        uncertain = next(row for row in report["rows"] if row["status"] == "uncertain")
        self.assertEqual(
            uncertain["timing"]["source"],
            "error.details.attempt_elapsed_seconds",
        )

    def test_visual_handoff_is_distinct_from_execution_evidence(self):
        handoff = follow(
            "a1", "Alice", status="uncertain", completed="2026-11-03T04:00:04Z",
            error={
                "code": "ACTION_UNCERTAIN",
                "message": "fixture",
                "details": {"visual_fallback": {"required": True}},
            },
            execution={"locator_strategy": "screenshot"},
        )
        report = audit_follow_run(ledger([handoff]))
        self.assertEqual(report["follow"]["handoff_requested"], 1)
        self.assertEqual(report["follow"]["visual_execution_claims"], 1)
        self.assertEqual(report["follow"]["visual_fallback_success_proven"], 0)
        self.assertIn("does not establish", report["visual_note"])

    def test_output_contains_no_private_targets(self):
        report = audit_follow_run(ledger([follow("a1", "PrivateHandle", elapsed=1)]))
        text = json.dumps(report, sort_keys=True)
        for secret in ("PrivateHandle", "https://x.com", "secret-claim", "secret-session"):
            self.assertNotIn(secret, text)
        self.assertTrue(report["rows"][0]["target_ref"])

    def test_action_result_mismatch_is_counted(self):
        data = ledger([follow("a1", "Alice", elapsed=1)])
        data["actions"]["a1"]["result"]["changed"] = False
        report = audit_follow_run(data)
        self.assertTrue(report["evidence"]["chain"]["valid"])
        self.assertEqual(
            report["evidence"]["comparison"]["result_evidence_mismatches"], 1
        )


if __name__ == "__main__":
    unittest.main()
