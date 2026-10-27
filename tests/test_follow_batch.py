import tempfile
import unittest

from browser_bridge import BrowserBridge, BridgeError
from follow_batch import FollowBatchPlanError, plan_batch


def targets(count=5, mutual=True):
    return [
        {
            "handle": f"user{i}",
            "profile_url": f"https://x.com/user{i}",
            "mutual_verified": mutual,
            "state": "follow_back_available",
        }
        for i in range(count)
    ]


class FollowBatchTests(unittest.TestCase):
    def test_candidate_observation_is_recorded_in_the_evidence_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, account_binding="owner-x")
            action = bridge.queue_observation(
                action_type="observe_followers",
                payload={"username": "owner", "limit": 2},
                session_id="s1",
            )
            claimed = bridge.next_action(session_id="s1", lease_seconds=60)
            result = bridge.complete_action(
                action_id=claimed["action_id"],
                claim_token=claimed["claim_token"],
                outcome="succeeded",
                result={
                    "owner_username": "owner",
                    "source_url": "https://x.com/owner/followers",
                    "targets": [
                        {
                            "handle": "user0",
                            "profile_url": "https://x.com/user0",
                            "mutual_verified": True,
                            "follows_you": True,
                            "state": "follow_back_available",
                        }
                    ],
                    "scrolls": 1,
                    "snapshot_sha256": "a" * 64,
                },
                error=None,
                session_id="s1",
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["result"]["mutual_count"], 1)
            self.assertEqual(bridge.evidence(limit=20)["valid"], True)

    def test_plan_is_read_only_and_chunked(self):
        plan = plan_batch(targets(200), chunk_size=20)
        self.assertEqual(plan["operation_count"], 200)
        self.assertEqual(plan["chunk_count"], 10)
        self.assertFalse(plan["execution"]["real_x_state_changed"])
        self.assertEqual(plan["operations"][0]["chunk"], 1)
        self.assertEqual(plan["operations"][-1]["chunk"], 10)

    def test_plan_requires_visible_mutual_proof(self):
        with self.assertRaises(FollowBatchPlanError):
            plan_batch(targets(1, mutual=False))

    def test_plan_skips_duplicate_and_already_following(self):
        data = targets(2)
        data.append({**data[0], "state": "already_following"})
        plan = plan_batch(data)
        self.assertEqual(plan["operation_count"], 2)

    def test_prepare_start_and_status_are_scoped_and_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, account_binding="owner-x")
            plan = plan_batch(targets(3))
            prepared = bridge.prepare_follow_batch(plan, session_id="s1")
            self.assertEqual(prepared["operation_count"], 3)
            with self.assertRaises(BridgeError) as caught:
                bridge.queue_follow_batch(prepared["batch_id"], "wrong", session_id="s1")
            self.assertEqual(caught.exception.code, "CONFIRMATION_MISMATCH")
            queued = bridge.queue_follow_batch(
                prepared["batch_id"], prepared["confirmation_phrase"], session_id="s1"
            )
            self.assertEqual(len(queued["action_ids"]), 3)
            action = bridge.next_action(
                batch_id=queued["batch_id"], chunk=1, session_id="s1", lease_seconds=60
            )
            self.assertEqual(action["type"], "follow_profile")
            self.assertEqual(action["payload"]["handle"], "user0")
            status = bridge.follow_batch_status(queued["batch_id"], session_id="s1")
            self.assertEqual(status["counts"]["claimed"], 1)

    def test_follow_result_requires_exact_target_and_verified_state(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory)
            plan = plan_batch(targets(1))
            prepared = bridge.prepare_follow_batch(plan, session_id="s1")
            queued = bridge.queue_follow_batch(
                prepared["batch_id"], prepared["confirmation_phrase"], session_id="s1"
            )
            action = bridge.next_action(batch_id=queued["batch_id"], session_id="s1", lease_seconds=60)
            with self.assertRaises(BridgeError) as caught:
                bridge.complete_action(
                    action_id=action["action_id"],
                    claim_token=action["claim_token"],
                    outcome="succeeded",
                    result={
                        "handle": "other",
                        "profile_url": "https://x.com/other",
                        "verified": True,
                    },
                    error=None,
                    session_id="s1",
                )
            self.assertEqual(caught.exception.code, "ACCOUNT_MISMATCH")


if __name__ == "__main__":
    unittest.main()
