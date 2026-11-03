"""Offline planning fixtures; no requests and no real X actions."""

import unittest
import tempfile
from unittest.mock import patch

from browser_bridge import BrowserBridge
from interaction_batch import BatchPlanError, plan_batch
from server import XStarter


class IndependentInteractionWaveTests(unittest.TestCase):
    def posts(self, count=10):
        return [
            {"url": f"https://x.com/source/status/{1000 + i}", "author": "source"}
            for i in range(count)
        ]

    def test_repost_only_wave_has_no_likes_or_duplicate_targets(self):
        plan = plan_batch(self.posts(), like_count=0, repost_count=10,
                          reply_count=0, reply_texts=[])
        self.assertEqual(plan["counts"], {"like": 0, "repost": 10, "reply": 0})
        self.assertEqual({op["action"] for op in plan["operations"]}, {"repost"})
        self.assertEqual(len({op["url"] for op in plan["operations"]}), 10)

    def test_reply_only_wave_preserves_supplied_drafts(self):
        texts = [f"Fixture-specific draft {i}" for i in range(10)]
        plan = plan_batch(self.posts(), like_count=0, repost_count=0,
                          reply_count=10, reply_texts=texts)
        self.assertEqual([op["text"] for op in plan["operations"]], texts)
        self.assertEqual({op["action"] for op in plan["operations"]}, {"reply"})

    def test_reposts_exceeding_like_count_stay_unique(self):
        plan = plan_batch(self.posts(), like_count=1, repost_count=10,
                          reply_count=0, reply_texts=[])
        reposts = [op for op in plan["operations"] if op["action"] == "repost"]
        self.assertEqual(len({op["url"] for op in reposts}), 10)

    def test_empty_wave_is_rejected(self):
        with self.assertRaises(BatchPlanError):
            plan_batch([], like_count=0, repost_count=0, reply_count=0, reply_texts=[])

    def test_prepare_queue_claim_support_each_independent_type(self):
        for kind in ("repost", "reply"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                bridge = BrowserBridge(directory, account_binding="fixture")
                plan = plan_batch(
                    self.posts(1), like_count=0,
                    repost_count=int(kind == "repost"), reply_count=int(kind == "reply"),
                    reply_texts=["Fixture draft"] if kind == "reply" else [],
                )
                prepared = bridge.prepare_interaction_batch(plan, session_id="s1")
                queued = bridge.queue_interaction_batch(
                    prepared["batch_id"], prepared["confirmation_phrase"], session_id="s1"
                )
                claimed = bridge.next_action(session_id="s1", batch_id=queued["batch_id"])
                self.assertEqual(claimed["type"], kind + "_post")
                self.assertEqual(len(bridge._read()["actions"]), 1)

    def test_mcp_plan_and_execute_preserve_headerless_rate_gate_for_each_type(self):
        for kind in ("like", "repost", "reply"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                app = XStarter(mode="browser", environ={
                    "X_STARTER_ENABLE_BROWSER": "1", "X_STARTER_STATE_DIR": directory,
                })
                self.addCleanup(app.close)
                plan = app._x_interaction_batch_plan({
                    "posts": self.posts(1), "like_count": int(kind == "like"),
                    "repost_count": int(kind == "repost"), "reply_count": int(kind == "reply"),
                    "reply_texts": ["Fixture draft"] if kind == "reply" else [],
                }, [])
                prepared = app._bridge().prepare_interaction_batch(plan, session_id="s1")
                ledger = app._bridge()._read()
                ledger["actions"]["rate"] = {
                    "type": "follow_profile", "status": "uncertain",
                    "completed_at": "2026-11-03T08:00:00Z",
                    "error": {"code": "X_RATE_LIMITED", "details": {}},
                }
                app._bridge()._write(ledger)
                with patch.object(app, "_browser_for") as browser:
                    result = app._x_interaction_batch_execute({
                        "batch_id": prepared["batch_id"],
                        "confirmation": prepared["confirmation_phrase"],
                        "_mcp_session_id": "s1", "failure_policy": "continue",
                    }, [])
                    browser.assert_not_called()
                self.assertEqual(result["processed"], 0)
                self.assertEqual(result["stop_reason"], "X_RATE_LIMITED")
                status = app._bridge().interaction_batch_status(
                    prepared["batch_id"], session_id="s1"
                )
                self.assertEqual(status["counts"]["pending"], 1)
                self.assertEqual(status["counts"]["claimed"], 0)

    def test_200_per_category_splits_without_incidental_actions(self):
        counts = {"like": 0, "repost": 0, "reply": 0}
        for action in counts:
            for offset in range(0, 200, 10):
                posts = [
                    {"url": f"https://x.com/u{offset+i}/status/{1000+offset+i}",
                     "author": f"u{offset+i}"} for i in range(10)
                ]
                plan = plan_batch(
                    posts, like_count=10 if action == "like" else 0,
                    repost_count=10 if action == "repost" else 0,
                    reply_count=10 if action == "reply" else 0,
                    reply_texts=[f"Fixture {offset+i}" for i in range(10)]
                    if action == "reply" else [],
                )
                for op in plan["operations"]:
                    self.assertEqual(op["action"], action)
                    counts[action] += 1
        self.assertEqual(counts, {"like": 200, "repost": 200, "reply": 200})


if __name__ == "__main__":
    unittest.main()
