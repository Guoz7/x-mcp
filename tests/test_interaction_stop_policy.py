"""Offline fixtures only: no X traffic or real account mutations."""

import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from browser_executor import Browser, ExecutorError
from interaction_batch import plan_batch
from server import StarterError, XStarter


class InteractionStopPolicyTests(unittest.TestCase):
    def _app_and_batch(self, directory):
        app = XStarter(mode="browser", environ={
            "X_STARTER_ENABLE_BROWSER": "1", "X_STARTER_STATE_DIR": directory,
        })
        self.addCleanup(app.close)
        posts = [{"url": f"https://x.com/u{i}/status/{1000+i}", "author": f"u{i}"} for i in range(3)]
        prepared = app._bridge().prepare_interaction_batch(
            plan_batch(posts, reply_texts=[], like_count=3, repost_count=0, reply_count=0),
            session_id="s1",
        )
        return app, prepared

    def _execute(self, app, prepared, **extra):
        return app._x_interaction_batch_execute({
            "batch_id": prepared["batch_id"], "confirmation": prepared["confirmation_phrase"],
            "_mcp_session_id": "s1", "failure_policy": "continue", **extra,
        }, [])

    def test_global_faults_stop_continue_after_one_action(self):
        for code in ("X_RATE_LIMITED", "X_SECURITY_INTERSTITIAL", "BROWSER_OWNER_EXCLUSIVE"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                app, prepared = self._app_and_batch(directory)
                with patch.object(app, "_browser_for") as browser_for:
                    browser_for.return_value.execute_x_action.side_effect = ExecutorError(code, "fixture")
                    data = self._execute(app, prepared)
                self.assertEqual(data["processed"], 1)
                self.assertEqual(data["stop_reason"], code)
                self.assertFalse(data["continued_after_uncertainty"])
                self.assertIn("attempt_elapsed_seconds", data["results"][0]["error"]["details"])
                status = app._bridge().interaction_batch_status(prepared["batch_id"], session_id="s1")
                self.assertEqual(status["remaining"], 2)

    def test_existing_rate_gate_blocks_before_browser_and_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            app, prepared = self._app_and_batch(directory)
            ledger = app._bridge()._read()
            ledger["actions"]["rate"] = {
                "type": "follow_profile", "status": "uncertain", "completed_at": "2026-11-03T08:00:00Z",
                "error": {"code": "X_RATE_LIMITED", "details": {}},
            }
            app._bridge()._write(ledger)
            with patch.object(app, "_browser_for") as browser_for:
                data = self._execute(app, prepared)
                browser_for.assert_not_called()
            self.assertEqual(data["processed"], 0)
            self.assertEqual(data["stop_reason"], "X_RATE_LIMITED")
            status = app._bridge().interaction_batch_status(prepared["batch_id"], session_id="s1")
            self.assertEqual(status["remaining"], 3)

    def test_owner_acquisition_fault_is_recorded_not_left_claimed(self):
        with tempfile.TemporaryDirectory() as directory:
            app, prepared = self._app_and_batch(directory)
            with patch.object(app, "_browser_for", side_effect=StarterError("BROWSER_OWNER_EXCLUSIVE", "fixture")):
                data = self._execute(app, prepared)
            self.assertEqual(data["stop_reason"], "BROWSER_OWNER_EXCLUSIVE")
            self.assertEqual(data["results"][0]["status"], "uncertain")

    def test_all_interaction_clicks_capture_429_without_second_click(self):
        for action_type in ("like_post", "repost_post", "reply_post"):
            with self.subTest(action_type=action_type):
                browser = Browser()
                browser.page = Mock()
                response = Mock(status=429)
                response.header_value.side_effect = lambda header: "60" if header == "retry-after" else None
                pending = Mock(value=response)
                browser.page.expect_response.return_value = MagicMock(__enter__=Mock(return_value=pending))
                control = Mock()
                with self.assertRaises(ExecutorError) as caught:
                    browser._click_x_write_once(control, action_type)
                self.assertEqual(caught.exception.code, "X_RATE_LIMITED")
                self.assertEqual(caught.exception.details["retry_after_seconds"], 60)
                control.click.assert_called_once()
                browser.page.reload.assert_not_called()

    def test_action_limit_leaves_remaining_targets_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            app, prepared = self._app_and_batch(directory)
            with patch.object(app, "_browser_for") as browser_for:
                browser_for.return_value.execute_x_action.side_effect = lambda **kw: {
                    "post_url": kw["post_url"], "verified": True, "changed": True,
                    "state": "liked", "locator_strategy": "dom",
                }
                data = self._execute(app, prepared, action_limit=1)
            self.assertEqual(data["processed"], 1)
            self.assertEqual(data["stop_reason"], "ACTION_LIMIT_REACHED")
            self.assertEqual(app._bridge().interaction_batch_status(prepared["batch_id"], session_id="s1")["remaining"], 2)

    def test_likes_reload_even_after_optimistic_success(self):
        browser = Browser()
        browser.page = Mock()
        browser._open_impl = Mock()
        browser._security_interstitial = Mock(return_value=False)
        browser._wait_for_initial_content = Mock()
        browser._consume_x_grant = Mock()
        browser._click_x_write_once = Mock()
        target = Mock()
        target.count.return_value = 1
        control = Mock()
        control.count.return_value = 1
        active = Mock()
        active.count.side_effect = [0, 1, 1]
        target.locator.side_effect = lambda selector: active if selector == '[data-testid="unlike"]' else control
        browser.page.locator.return_value = target
        result = browser._execute_x_action_impl(
            action_type="like_post", post_url="https://x.com/u/status/1000", author="u",
            text=None, action_id="a", claim_token="fixture", lease_expires_epoch=9999999999,
        )
        self.assertTrue(result["verified"])
        browser.page.reload.assert_called_once()
        browser._click_x_write_once.assert_called_once_with(control, "like_post")

    def test_resume_requires_same_confirmation_and_skips_completed_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            app, prepared = self._app_and_batch(directory)
            with patch.object(app, "_browser_for") as browser_for:
                browser_for.return_value.execute_x_action.side_effect = lambda **kw: {
                    "post_url": kw["post_url"], "verified": True, "changed": True,
                    "state": "liked", "locator_strategy": "dom",
                }
                self._execute(app, prepared, action_limit=1)
                bad = {**prepared, "confirmation_phrase": "wrong"}
                with self.assertRaises(StarterError) as caught:
                    self._execute(app, bad)
                self.assertEqual(caught.exception.code, "CONFIRMATION_MISMATCH")
                data = self._execute(app, prepared)
                self.assertEqual(data["processed"], 2)
                self.assertEqual(browser_for.return_value.execute_x_action.call_count, 3)

    def test_deadline_checked_before_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            app, prepared = self._app_and_batch(directory)
            with patch.object(app, "_browser_for") as browser_for:
                with patch("server.time.monotonic", side_effect=[0, 2, 2]):
                    data = self._execute(app, prepared, deadline_seconds=1)
                browser_for.assert_not_called()
            self.assertEqual(data["processed"], 0)
            self.assertEqual(data["stop_reason"], "DEADLINE_REACHED")


if __name__ == "__main__":
    unittest.main()
