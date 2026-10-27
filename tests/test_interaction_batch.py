import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from interaction_batch import BatchPlanError, plan_batch
from browser_bridge import BrowserBridge, BridgeError
from browser_executor import Browser, ExecutorError


def make_posts(count=40):
    return [
        {"url": f"https://x.com/user{i}/status/{1000 + i}", "author": f"user{i}", "text": f"post {i}"}
        for i in range(count)
    ]


class InteractionBatchTests(unittest.TestCase):
    def test_default_demo_shape_is_bounded_and_deterministic(self):
        replies = [f"Reply {i}" for i in range(10)]
        first = plan_batch(make_posts(), reply_texts=replies)
        self.assertEqual(first, plan_batch(make_posts(), reply_texts=replies))
        self.assertEqual(first["operation_count"], 31)
        self.assertEqual(first["counts"], {"like": 20, "repost": 1, "reply": 10})
        self.assertEqual(first["distinct_like_authors"], 20)
        self.assertEqual([item["sequence"] for item in first["operations"]], list(range(1, 32)))
        self.assertFalse(first["execution"]["real_x_state_changed"])
        self.assertEqual(first["execution"]["one_minute_sla"], "not_verified")

    def test_planning_benchmark_is_not_live_execution(self):
        started = time.perf_counter()
        for _ in range(1000):
            plan_batch(make_posts(), reply_texts=["ok"] * 10)
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_distinct_authors_are_required_for_likes(self):
        data = make_posts()
        for item in data[1:]:
            item["author"] = "same"
        with self.assertRaises(BatchPlanError):
            plan_batch(data, reply_texts=["ok"] * 10)

    def test_short_reply_list_is_rejected(self):
        with self.assertRaises(BatchPlanError):
            plan_batch(make_posts(), reply_texts=["only one"])

    def test_large_batch_is_split_into_confirmed_chunks(self):
        posts = make_posts(420)
        plan = plan_batch(
            posts,
            reply_texts=[f"reply {i}" for i in range(200)],
            like_count=200,
            repost_count=10,
            reply_count=200,
            chunk_size=20,
        )
        self.assertEqual(plan["operation_count"], 410)
        self.assertEqual(plan["counts"], {"like": 200, "repost": 10, "reply": 200})
        self.assertEqual(plan["chunk_size"], 20)
        self.assertEqual(plan["chunk_count"], 21)
        self.assertEqual(plan["operations"][0]["chunk"], 1)
        self.assertEqual(plan["operations"][-1]["chunk"], 21)
        self.assertEqual(
            [item["sequence"] for item in plan["operations"]],
            list(range(1, 411)),
        )

    def test_chunk_claims_and_status_are_grouped(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, account_binding="test")
            plan = plan_batch(
                make_posts(60),
                reply_texts=[f"reply {i}" for i in range(10)],
                like_count=40,
                repost_count=0,
                reply_count=10,
                chunk_size=20,
            )
            prepared = bridge.prepare_interaction_batch(plan, session_id="s1")
            queued = bridge.queue_interaction_batch(
                prepared["batch_id"], prepared["confirmation_phrase"], session_id="s1"
            )
            self.assertEqual(len(queued["action_ids"]), 50)

            claimed = bridge.next_action(
                lease_seconds=60,
                session_id="s1",
                batch_id=queued["batch_id"],
                chunk=2,
            )
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["payload"]["chunk"], 2)
            self.assertEqual(claimed["payload"]["sequence"], 21)

            status = bridge.interaction_batch_status(queued["batch_id"], session_id="s1")
            self.assertEqual([item["chunk"] for item in status["chunks"]], [1, 2, 3])
            self.assertEqual(status["chunks"][1]["planned"], 20)
            self.assertEqual(status["chunks"][1]["claimed"], 1)
            self.assertEqual(status["chunks"][1]["pending"], 19)
            self.assertEqual(status["remaining"], 50)

    def test_chunk_scoped_claim_skips_other_chunks(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, account_binding="test")
            plan = plan_batch(
                make_posts(50),
                reply_texts=[],
                like_count=45,
                repost_count=0,
                reply_count=0,
                chunk_size=20,
            )
            prepared = bridge.prepare_interaction_batch(plan, session_id="s1")
            queued = bridge.queue_interaction_batch(
                prepared["batch_id"], prepared["confirmation_phrase"], session_id="s1"
            )
            seen = []
            while True:
                action = bridge.next_action(
                    lease_seconds=60,
                    session_id="s1",
                    batch_id=queued["batch_id"],
                    chunk=3,
                )
                if action is None:
                    break
                seen.append(action["payload"]["sequence"])
            self.assertEqual(seen, [41, 42, 43, 44, 45])

    def test_prepare_and_start_queues_31_scoped_actions(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, account_binding="test")
            plan = plan_batch(make_posts(), reply_texts=["ok"] * 10)
            prepared = bridge.prepare_interaction_batch(plan, session_id="s1")
            started = bridge.queue_interaction_batch(
                prepared["batch_id"], prepared["confirmation_phrase"], session_id="s1"
            )
            self.assertEqual(started["operation_count"], 31)
            self.assertEqual(len(started["action_ids"]), 31)
            action_types = [bridge.get_action(item)["type"] for item in started["action_ids"]]
            self.assertEqual(action_types.count("like_post"), 20)
            self.assertEqual(action_types.count("repost_post"), 1)
            self.assertEqual(action_types.count("reply_post"), 10)

    def test_batch_confirmation_is_exact_and_single_use(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, account_binding="test")
            plan = plan_batch(make_posts(), reply_texts=["ok"] * 10)
            prepared = bridge.prepare_interaction_batch(plan, session_id="s1")
            with self.assertRaises(BridgeError) as caught:
                bridge.queue_interaction_batch(prepared["batch_id"], "wrong", session_id="s1")
            self.assertEqual(caught.exception.code, "CONFIRMATION_MISMATCH")
            bridge.queue_interaction_batch(prepared["batch_id"], prepared["confirmation_phrase"], session_id="s1")
            with self.assertRaises(BridgeError) as caught:
                bridge.queue_interaction_batch(prepared["batch_id"], prepared["confirmation_phrase"], session_id="s1")
            self.assertEqual(caught.exception.code, "BATCH_NOT_READY")

    def test_parallel_batch_claims_are_scoped_to_the_requested_batch(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, account_binding="test")
            plan_a = plan_batch(make_posts(), reply_texts=["a"] * 10)
            plan_b = plan_batch(
                [{**post, "url": post["url"].replace("user", "other")} for post in make_posts()],
                reply_texts=["b"] * 10,
            )
            prepared_a = bridge.prepare_interaction_batch(plan_a, session_id="s1")
            prepared_b = bridge.prepare_interaction_batch(plan_b, session_id="s1")
            queued_a = bridge.queue_interaction_batch(
                prepared_a["batch_id"], prepared_a["confirmation_phrase"], session_id="s1"
            )
            queued_b = bridge.queue_interaction_batch(
                prepared_b["batch_id"], prepared_b["confirmation_phrase"], session_id="s1"
            )

            first_a = bridge.next_action(
                lease_seconds=60, session_id="s1", batch_id=queued_a["batch_id"]
            )
            first_b = bridge.next_action(
                lease_seconds=60, session_id="s1", batch_id=queued_b["batch_id"]
            )
            self.assertIsNotNone(first_a)
            self.assertIsNotNone(first_b)
            self.assertEqual(first_a["payload"]["batch_id"], queued_a["batch_id"])
            self.assertEqual(first_b["payload"]["batch_id"], queued_b["batch_id"])

    def test_batch_claims_follow_plan_sequence_not_sorted_json_keys(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, account_binding="test")
            plan = plan_batch(make_posts(), reply_texts=[], like_count=3, repost_count=0, reply_count=0)
            prepared = bridge.prepare_interaction_batch(plan, session_id="s1")
            queued = bridge.queue_interaction_batch(
                prepared["batch_id"], prepared["confirmation_phrase"], session_id="s1"
            )
            claimed = [
                bridge.next_action(lease_seconds=60, session_id="s1", batch_id=queued["batch_id"])
                for _ in range(3)
            ]
            self.assertEqual(
                [item["payload"]["sequence"] for item in claimed if item is not None],
                [1, 2, 3],
            )

    def test_batch_resume_does_not_duplicate_actions(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, account_binding="test")
            plan = plan_batch(make_posts(), reply_texts=[], like_count=1, repost_count=0, reply_count=0)
            prepared = bridge.prepare_interaction_batch(plan, session_id="s1")
            first = bridge.queue_interaction_batch(
                prepared["batch_id"], prepared["confirmation_phrase"], session_id="s1"
            )
            with self.assertRaises(BridgeError) as caught:
                bridge.queue_interaction_batch(
                    prepared["batch_id"], prepared["confirmation_phrase"], session_id="s1"
                )
            self.assertEqual(caught.exception.code, "BATCH_NOT_READY")
            self.assertEqual(len(bridge._read()["actions"]), 1)

    def test_batch_status_reports_progress_and_changed_count(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, account_binding="test")
            plan = plan_batch(make_posts(), reply_texts=[], like_count=1, repost_count=0, reply_count=0)
            prepared = bridge.prepare_interaction_batch(plan, session_id="s1")
            queued = bridge.queue_interaction_batch(
                prepared["batch_id"], prepared["confirmation_phrase"], session_id="s1"
            )
            status = bridge.interaction_batch_status(queued["batch_id"], session_id="s1")
            self.assertEqual(status["state"], "queued")
            self.assertEqual(status["remaining"], 1)

    def test_visual_executor_grants_cover_all_interaction_types(self):
        browser = Browser()
        for action_type in ("like_post", "repost_post", "reply_post"):
            browser.x_grant = None
            grant = browser.authorize_x_action(
                action_id="act_" + action_type,
                claim_token="t" * 32,
                lease_expires_epoch=time.time() + 60,
                action_type=action_type,
            )
            self.assertTrue(grant["authorized"])
            self.assertEqual(grant["stages"]["click"], 2 if action_type == "repost_post" else 1)
            if action_type == "reply_post":
                self.assertEqual(grant["stages"]["typing"], 1)
            else:
                self.assertEqual(grant["stages"]["typing"], 0)
        browser.close()


if __name__ == "__main__":
    unittest.main()
