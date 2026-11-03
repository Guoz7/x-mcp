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

    def test_resume_confirmation_is_required_for_queued_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, account_binding="owner-x")
            prepared = bridge.prepare_follow_batch(plan_batch(targets(1)), session_id="s1")
            queued = bridge.queue_follow_batch(
                prepared["batch_id"], prepared["confirmation_phrase"], session_id="s1"
            )
            with self.assertRaises(BridgeError) as caught:
                bridge.validate_follow_batch_confirmation(
                    queued["batch_id"], "FOLLOW WRONG", session_id="s1"
                )
            self.assertEqual(caught.exception.code, "CONFIRMATION_MISMATCH")

    def test_planned_batch_survives_bridge_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            first = BrowserBridge(directory, account_binding="owner-x")
            plan = plan_batch(targets(2))
            first.save_follow_plan(plan, session_id="s1")

            reopened = BrowserBridge(directory, account_binding="owner-x")
            loaded = reopened.get_follow_plan(plan["batch_id"], session_id="new-session")
            self.assertIsNotNone(loaded)
            prepared = reopened.prepare_follow_batch(loaded, session_id="s1")
            self.assertEqual(prepared["operation_count"], 2)
            self.assertIsNone(reopened.get_follow_plan(plan["batch_id"], session_id="s1"))

    def test_compact_plan_summary_recovers_persisted_operations(self):
        from server import XStarter

        with tempfile.TemporaryDirectory() as directory:
            app = XStarter(
                mode="browser",
                environ={
                    "X_STARTER_ENABLE_BROWSER": "1",
                    "X_STARTER_STATE_DIR": directory,
                },
            )
            try:
                plan = plan_batch(targets(2))
                app._bridge().save_follow_plan(plan, session_id="s1")
                prepared = app._x_follow_batch_prepare(
                    {
                        "plan": {
                            "batch_id": plan["batch_id"],
                            "status": "planned_not_executed",
                            "operation_count": 2,
                        },
                        "_mcp_session_id": "s1",
                    },
                    [],
                )
                self.assertEqual(prepared["operation_count"], 2)
            finally:
                app.close()

    def test_scoped_batch_is_not_visible_to_unscoped_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory, account_binding="owner-x")
            prepared = bridge.prepare_follow_batch(plan_batch(targets(1)), session_id="s1")
            with self.assertRaises(BridgeError) as caught:
                bridge.follow_batch_status(prepared["batch_id"])
            self.assertEqual(caught.exception.code, "BATCH_NOT_FOUND")

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

    def test_follow_result_keeps_browser_action_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = BrowserBridge(directory)
            prepared = bridge.prepare_follow_batch(plan_batch(targets(1)), session_id="s1")
            queued = bridge.queue_follow_batch(
                prepared["batch_id"], prepared["confirmation_phrase"], session_id="s1"
            )
            action = bridge.next_action(
                batch_id=queued["batch_id"], session_id="s1", lease_seconds=60
            )
            completed = bridge.complete_action(
                action_id=action["action_id"],
                claim_token=action["claim_token"],
                outcome="succeeded",
                result={
                    "handle": "user0",
                    "profile_url": "https://x.com/user0",
                    "verified": True,
                    "state": "following",
                    "changed": True,
                    "elapsed_seconds": 4.321,
                    "api_response": {
                        "method": "POST",
                        "path": "/i/api/1.1/friendships/create.json",
                        "status": 200,
                        "authorization": "must-not-survive",
                    },
                },
                error=None,
                session_id="s1",
            )
            self.assertEqual(completed["result"]["elapsed_seconds"], 4.321)
            self.assertEqual(completed["result"]["api_response"]["status"], 200)
            self.assertNotIn("authorization", completed["result"]["api_response"])

    def test_execute_tool_records_success_without_name_error(self):
        # Regression: the success path referenced an undefined browser_family,
        # so every verified follow was recorded as uncertain and the batch stopped.
        from unittest.mock import patch

        from server import XStarter

        app = XStarter(mode="browser", environ={"X_STARTER_ENABLE_BROWSER": "1"})
        try:
            prepared = app._bridge().prepare_follow_batch(plan_batch(targets(2)), session_id="s1")

            def follow(**kwargs):
                handle = kwargs["author"]
                return {
                    "handle": handle,
                    "profile_url": f"https://x.com/{handle}",
                    "verified": True,
                    "state": "following",
                    "changed": True,
                    "locator_strategy": "dom",
                }

            with patch.object(app, "_browser_for") as browser_for:
                browser_for.return_value.browser_family = "chromium"
                browser_for.return_value.execute_x_action.side_effect = follow
                data = app._x_follow_batch_execute(
                    {
                        "batch_id": prepared["batch_id"],
                        "confirmation": prepared["confirmation_phrase"],
                        "_mcp_session_id": "s1",
                    },
                    [],
                )
            self.assertEqual(data["processed"], 2)
            self.assertEqual([item["status"] for item in data["results"]], ["completed", "completed"])
            self.assertFalse(data["stopped_on_uncertainty"])
        finally:
            app.close()

    def test_execute_tool_stops_at_verified_success_limit(self):
        from unittest.mock import patch

        from server import XStarter

        app = XStarter(mode="browser", environ={"X_STARTER_ENABLE_BROWSER": "1"})
        try:
            prepared = app._bridge().prepare_follow_batch(plan_batch(targets(5)), session_id="s1")

            def follow(**kwargs):
                handle = kwargs["author"]
                return {
                    "handle": handle,
                    "profile_url": f"https://x.com/{handle}",
                    "verified": True,
                    "state": "following",
                    "changed": True,
                    "locator_strategy": "dom",
                }

            with patch.object(app, "_browser_for") as browser_for:
                browser_for.return_value.browser_family = "chromium"
                browser_for.return_value.execute_x_action.side_effect = follow
                data = app._x_follow_batch_execute(
                    {
                        "batch_id": prepared["batch_id"],
                        "confirmation": prepared["confirmation_phrase"],
                        "failure_policy": "continue",
                        "success_limit": 3,
                        "_mcp_session_id": "s1",
                    },
                    [],
                )
            self.assertEqual(data["processed"], 3)
            self.assertEqual(
                [item["status"] for item in data["results"]],
                ["completed", "completed", "completed"],
            )
            self.assertEqual(data["verified_changed"], 3)
            self.assertTrue(data["success_limit_reached"])
            self.assertFalse(data["deadline_reached"])
        finally:
            app.close()

    def test_execute_tool_checks_deadline_before_claiming(self):
        from unittest.mock import patch

        from server import XStarter

        app = XStarter(mode="browser", environ={"X_STARTER_ENABLE_BROWSER": "1"})
        try:
            prepared = app._bridge().prepare_follow_batch(plan_batch(targets(2)), session_id="s1")
            clock = iter([0.0, 2.0])

            def monotonic():
                return next(clock, 2.0)

            with patch("server.time.monotonic", side_effect=monotonic):
                data = app._x_follow_batch_execute(
                    {
                        "batch_id": prepared["batch_id"],
                        "confirmation": prepared["confirmation_phrase"],
                        "deadline_seconds": 1,
                        "_mcp_session_id": "s1",
                    },
                    [],
                )
            self.assertEqual(data["processed"], 0)
            self.assertTrue(data["deadline_reached"])
            self.assertFalse(data["success_limit_reached"])
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
