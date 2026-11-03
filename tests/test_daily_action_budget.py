"""Synthetic-ledger tests for daily_action_budget.

These fixtures are hand-built examples, not live X evidence. The tests do not
open a browser, read a real state file, use the network, sleep, or schedule a
job.
"""

import hashlib
import unittest
from datetime import date

from daily_action_budget import (
    ACTION_TYPES,
    DAILY_GOAL,
    evaluate_daily_action_budget,
)


DAY = date(2026, 11, 3)
COMPLETED_AT = "2026-11-03T04:00:00Z"  # 12:00 Asia/Shanghai
HASH = "a" * 64


def action(
    action_type,
    *,
    target="7",
    completed_at=COMPLETED_AT,
    status="completed",
    verified=True,
    changed=True,
    state=None,
):
    author = "source"
    payload = {
        "post_url": f"https://x.com/{author}/status/{target}",
        "author": author,
    }
    result = {
        "post_url": payload["post_url"],
        "author": author,
        "verified": verified,
        "changed": changed,
    }
    if action_type == "follow_profile":
        payload = {
            "handle": "target",
            "profile_url": "https://x.com/target",
        }
        result = {
            "handle": "target",
            "profile_url": "https://x.com/target",
            "verified": verified,
            "changed": changed,
        }
    if action_type == "reply_post":
        payload["text"] = "Fixture reply"
        result.update(
            reply_url=f"https://x.com/owner/status/{int(target) + 1000}",
            text=payload["text"],
            text_sha256=hashlib.sha256(payload["text"].encode("utf-8")).hexdigest(),
        )
    if state is not None:
        result["state"] = state
    return {
        "type": action_type,
        "status": status,
        "payload": payload,
        "result": result,
        "completed_at": completed_at,
    }


def fixture_ledger(*actions):
    return {
        "schema": "x-starter-browser-bridge/v1",
        "account_binding": "Owner",
        "actions": {
            f"act_{index}": item for index, item in enumerate(actions)
        },
    }


class DailyActionBudgetTests(unittest.TestCase):
    def test_counts_only_strictly_verified_changed_states(self):
        ledger = fixture_ledger(
            action("follow_profile", state="following"),
            action("like_post", state="liked"),
            action("repost_post", state="reposted"),
            action("reply_post"),
            action("like_post", target="8", status="claimed", state="liked"),
            action("like_post", target="9", status="uncertain", state="liked"),
            action("like_post", target="10", changed=False, state="liked"),
            action("like_post", target="11", verified=False, state="liked"),
            action("like_post", target="12", state="active"),
        )
        report = evaluate_daily_action_budget(
            ledger, local_day=DAY, now_epoch=1793683200
        )
        self.assertEqual(report["account_binding"], "Owner")
        self.assertEqual(
            report["achieved"],
            {
                "follow_profile": 1,
                "like_post": 1,
                "repost_post": 1,
                "reply_post": 1,
            },
        )
        self.assertEqual(report["skipped"]["ineligible"], 5)
        self.assertEqual(report["total_achieved"], 4)

    def test_deduplicates_once_per_account_type_and_target(self):
        ledger = fixture_ledger(
            action("like_post", target="7", state="liked"),
            action("like_post", target="7", state="liked"),
            action("repost_post", target="7", state="reposted"),
        )
        report = evaluate_daily_action_budget(
            ledger, local_day=DAY, now_epoch=1793683200
        )
        self.assertEqual(report["achieved"]["like_post"], 1)
        self.assertEqual(report["achieved"]["repost_post"], 1)
        self.assertEqual(report["skipped"]["duplicate"], 1)

    def test_reply_requires_canonical_url_and_exact_text_proof(self):
        valid = action("reply_post", target="20")
        no_url = action("reply_post", target="21")
        no_url["result"].pop("reply_url")
        wrong_text = action("reply_post", target="22")
        wrong_text["result"]["text"] = "Different"
        wrong_hash = action("reply_post", target="23")
        wrong_hash["result"]["text_sha256"] = HASH
        report = evaluate_daily_action_budget(
            fixture_ledger(valid, no_url, wrong_text, wrong_hash),
            local_day=DAY,
            now_epoch=1793683200,
        )
        self.assertEqual(report["achieved"]["reply_post"], 1)
        self.assertEqual(report["skipped"]["ineligible"], 3)

    def test_reply_deduplicates_by_reply_url(self):
        first = action("reply_post", target="30")
        second = action("reply_post", target="31")
        second["result"]["reply_url"] = first["result"]["reply_url"]
        report = evaluate_daily_action_budget(
            fixture_ledger(first, second),
            local_day=DAY,
            now_epoch=1793683200,
        )
        self.assertEqual(report["achieved"]["reply_post"], 1)
        self.assertEqual(report["skipped"]["duplicate"], 1)

    def test_cross_day_uses_asia_shanghai_boundary(self):
        # 2026-11-03T16:30:00Z is 2026-11-04 00:30 in Shanghai.
        report = evaluate_daily_action_budget(
            fixture_ledger(
                action("like_post", completed_at="2026-11-03T15:59:59Z", state="liked"),
                action("like_post", target="8", completed_at="2026-11-03T16:30:00Z", state="liked"),
            ),
            local_day=date(2026, 11, 4),
            now_epoch=1793720400,
        )
        self.assertEqual(report["achieved"]["like_post"], 1)
        self.assertEqual(report["skipped"]["outside_local_day"], 1)

    def test_missing_account_binding_fails_closed(self):
        ledger = fixture_ledger(action("like_post", state="liked"))
        ledger["account_binding"] = ""
        report = evaluate_daily_action_budget(
            ledger, local_day=DAY, now_epoch=1793683200
        )
        self.assertEqual(report["achieved"]["like_post"], 0)
        self.assertTrue(report["blocked"])
        self.assertEqual(report["blocked_detail"]["reason"], "account_binding_missing")

    def test_rate_gate_comes_from_the_durable_ledger(self):
        ledger = fixture_ledger(
            {
                "type": "follow_profile",
                "status": "uncertain",
                "payload": {},
                "result": None,
                "completed_at": "2026-11-03T04:00:00Z",
                "error": {
                    "code": "X_RATE_LIMITED",
                    "details": {"retry_after_seconds": 60},
                },
            }
        )
        report = evaluate_daily_action_budget(
            ledger, local_day=DAY, now_epoch=1793678400
        )
        self.assertTrue(report["blocked"])
        self.assertEqual(
            report["policy_source"], "follow_orchestration.derive_from_ledger"
        )
        self.assertEqual(report["blocked_detail"]["reason"], "cooldown_active")
        self.assertEqual(report["blocked_detail"]["wait_until_epoch"], 1793678460)
        self.assertEqual(report["achieved"]["follow_profile"], 0)

    def test_success_only_ledger_keeps_the_rate_gate_clear(self):
        report = evaluate_daily_action_budget(
            fixture_ledger(action("like_post", state="liked")),
            local_day=DAY,
            now_epoch=1793678430,
        )
        self.assertFalse(report["blocked"])
        self.assertEqual(report["policy_source"], "follow_orchestration.derive_from_ledger")

    def test_each_action_type_has_its_own_goal(self):
        actions = [
            action("like_post", target=str(index + 100), state="liked")
            for index in range(DAILY_GOAL)
        ]
        report = evaluate_daily_action_budget(
            fixture_ledger(*actions), local_day=DAY, now_epoch=1793683200
        )
        self.assertEqual(report["action_types"], list(ACTION_TYPES))
        self.assertEqual(report["achieved"]["like_post"], DAILY_GOAL)
        self.assertEqual(report["remaining"]["like_post"], 0)
        self.assertEqual(report["remaining"]["follow_profile"], DAILY_GOAL)
        self.assertFalse(report["goal_met"])


if __name__ == "__main__":
    unittest.main()
