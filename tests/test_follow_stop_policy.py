"""Bounded tests for follow-batch stop policy with a mock browser.

These tests use an isolated temporary state directory. They never start a
browser or make a network request.
"""

import itertools
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

from browser_executor import Browser, ExecutorError
from follow_batch import plan_batch
from server import StarterError, XStarter


GLOBAL_STOP_CODES = (
    "X_SECURITY_INTERSTITIAL",
    "X_RATE_LIMITED",
    "BROWSER_RUNTIME_MISSING",
    "BROWSER_BINARY_MISSING",
    "BROWSER_START_FAILED",
    "BROWSER_EXECUTOR_TIMEOUT",
    "BROWSER_EXECUTOR_STUCK",
    "BROWSER_SESSION_BUSY",
    "BROWSER_OWNER_EXCLUSIVE",
)


def targets(count=3):
    return [
        {
            "handle": f"user{i}",
            "profile_url": f"https://x.com/user{i}",
            "mutual_verified": True,
            "state": "follow_back_available",
        }
        for i in range(count)
    ]


def verified_follow(handle):
    return {
        "handle": handle,
        "profile_url": f"https://x.com/{handle}",
        "verified": True,
        "state": "following",
        "changed": True,
        "locator_strategy": "dom",
    }


class _FakePlaywrightTimeoutError(Exception):
    pass


@contextmanager
def fake_playwright():
    """Supply the lazy TimeoutError import without installing Playwright."""

    package = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.TimeoutError = _FakePlaywrightTimeoutError
    package.sync_api = sync_api
    with patch.dict(
        sys.modules,
        {"playwright": package, "playwright.sync_api": sync_api},
    ):
        yield


class FollowStopPolicyTests(unittest.TestCase):
    def test_durable_gate_stops_before_claim_or_browser_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory)
            first = self._prepared_batch(app)
            with patch.object(app, "_browser_for") as browser_for:
                browser_for.return_value.browser_family = "chromium"
                browser_for.return_value.execute_x_action.side_effect = ExecutorError(
                    "X_RATE_LIMITED", "mock 429"
                )
                self._execute(app, first, "continue")
            second = self._prepared_batch(app)
            with patch.object(app, "_browser_for") as browser_for:
                data = self._execute(app, second, "continue")
                browser_for.assert_not_called()
            self.assertEqual(data["processed"], 0)
            self.assertEqual(data["stop_reason"], "X_RATE_LIMITED")
            self.assertEqual(data["stop_details"]["recovery"], "manual_recovery_required")
            status = app._bridge().follow_batch_status(second["batch_id"], session_id="s1")
            self.assertEqual(status["counts"]["pending"], 3)
            self.assertEqual(status["counts"]["claimed"], 0)

    def _app(self, directory):
        app = XStarter(
            mode="browser",
            environ={
                "X_STARTER_ENABLE_BROWSER": "1",
                "X_STARTER_STATE_DIR": directory,
            },
        )
        self.addCleanup(app.close)
        return app

    def _prepared_batch(self, app):
        return app._bridge().prepare_follow_batch(
            plan_batch(targets(3)), session_id="s1"
        )

    def _execute(self, app, prepared, failure_policy):
        return app._x_follow_batch_execute(
            {
                "batch_id": prepared["batch_id"],
                "confirmation": prepared["confirmation_phrase"],
                "failure_policy": failure_policy,
                "_mcp_session_id": "s1",
            },
            [],
        )

    def _assert_timing(self, error):
        self.assertIn("attempt_elapsed_seconds", error["details"])
        self.assertIsInstance(error["details"]["attempt_elapsed_seconds"], float)
        self.assertGreaterEqual(error["details"]["attempt_elapsed_seconds"], 0.0)

    def test_global_failure_stops_continue_policy_after_one_target(self):
        for code in GLOBAL_STOP_CODES:
            with self.subTest(code=code):
                with tempfile.TemporaryDirectory() as directory:
                    app = self._app(directory)
                    prepared = self._prepared_batch(app)

                    with patch.object(app, "_browser_for") as browser_for:
                        browser_for.return_value.browser_family = "chromium"
                        browser_for.return_value.execute_x_action.side_effect = (
                            ExecutorError(code, f"mock {code}")
                        )
                        data = self._execute(app, prepared, "continue")

                    self.assertIn("stop_reason", data)
                    self.assertEqual(data["stop_reason"], code)
                    self.assertFalse(data["continued_after_uncertainty"])
                    self.assertEqual(data["processed"], 1)
                    self.assertEqual(
                        [item["status"] for item in data["results"]],
                        ["uncertain"],
                    )
                    error = data["results"][0]["error"]
                    self.assertEqual(error["code"], code)
                    self._assert_timing(error)
                    self.assertEqual(
                        browser_for.return_value.execute_x_action.call_count, 1
                    )

                    status = app._bridge().follow_batch_status(
                        prepared["batch_id"], session_id="s1"
                    )
                    self.assertEqual(status["counts"]["pending"], 2)
                    self.assertEqual(status["remaining"], 2)

    def test_local_action_uncertain_continues_to_remaining_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory)
            prepared = self._prepared_batch(app)
            calls = {"count": 0}

            def follow(**kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise ExecutorError(
                        "ACTION_UNCERTAIN", "mock local action uncertainty"
                    )
                return verified_follow(kwargs["author"])

            with patch.object(app, "_browser_for") as browser_for:
                browser_for.return_value.browser_family = "chromium"
                browser_for.return_value.execute_x_action.side_effect = follow
                data = self._execute(app, prepared, "continue")

            self.assertIn("stop_reason", data)
            self.assertIsNone(data["stop_reason"])
            self.assertTrue(data["continued_after_uncertainty"])
            self.assertEqual(data["processed"], 3)
            self.assertEqual(
                [item["status"] for item in data["results"]],
                ["uncertain", "completed", "completed"],
            )
            error = data["results"][0]["error"]
            self.assertEqual(error["code"], "ACTION_UNCERTAIN")
            self._assert_timing(error)
            self.assertEqual(data["verified_changed"], 2)
            self.assertEqual(
                browser_for.return_value.execute_x_action.call_count, 3
            )

            status = app._bridge().follow_batch_status(
                prepared["batch_id"], session_id="s1"
            )
            self.assertEqual(status["counts"]["uncertain"], 1)
            self.assertEqual(status["counts"]["completed"], 2)
            self.assertEqual(status["counts"]["pending"], 0)
            self.assertEqual(status["remaining"], 0)
            self.assertEqual(status["changed"], 2)

    def test_owner_exclusive_marks_claimed_action_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory)
            prepared = self._prepared_batch(app)

            with patch.object(
                app,
                "_browser_for",
                side_effect=StarterError(
                    "BROWSER_OWNER_EXCLUSIVE",
                    "The owner browser is already used by another MCP session.",
                ),
            ):
                data = self._execute(app, prepared, "continue")

            self.assertEqual(data["processed"], 1)
            self.assertEqual(data["stop_reason"], "BROWSER_OWNER_EXCLUSIVE")
            self.assertEqual(
                [item["status"] for item in data["results"]],
                ["uncertain"],
            )
            error = data["results"][0]["error"]
            self.assertEqual(error["code"], "BROWSER_OWNER_EXCLUSIVE")
            self._assert_timing(error)

            status = app._bridge().follow_batch_status(
                prepared["batch_id"], session_id="s1"
            )
            self.assertEqual(status["counts"]["claimed"], 0)
            self.assertEqual(status["counts"]["uncertain"], 1)
            self.assertEqual(status["counts"]["pending"], 2)
            self.assertEqual(status["remaining"], 2)
            self.assertEqual(
                status["results"][0]["status"],
                "uncertain",
            )
            self.assertEqual(
                status["results"][0]["error"]["code"],
                "BROWSER_OWNER_EXCLUSIVE",
            )
            self._assert_timing(status["results"][0]["error"])

    def test_follow_executor_classifies_429_as_rate_limited(self):
        browser = Browser()
        browser.page = Mock()
        browser.page.evaluate.return_value = {
            "follow_testid": "123-follow",
            "unfollow_testid": None,
            "pending": False,
            "target_mismatch": False,
            "reason": "ok",
        }
        browser._security_interstitial = Mock(return_value=False)
        browser._consume_x_grant = Mock()
        response = Mock(status=429)
        response.request.method = "POST"
        response.header_value.return_value = "60"
        wait = Mock()
        wait.value = response
        browser.page.expect_response.return_value = MagicMock(
            __enter__=Mock(return_value=wait)
        )

        with fake_playwright(), self.assertRaises(ExecutorError) as caught:
            browser._execute_follow_profile_impl(
                profile_url="https://x.com/user0", handle="user0"
            )

        error = caught.exception
        self.assertEqual(error.code, "X_RATE_LIMITED")
        self.assertEqual(error.details["api_response"]["status"], 429)
        self.assertEqual(error.details["retry_after_seconds"], 60)
        self.assertTrue(error.details["write_dispatched"])
        self.assertIn("attempt_elapsed_seconds", error.details)
        browser.page.locator.return_value.click.assert_called_once()
        browser.page.reload.assert_not_called()

    def test_post_write_visual_fallback_keeps_api_status_and_forbids_second_click(
        self,
    ):
        browser = Browser()
        browser.page = Mock()
        browser.page.evaluate.return_value = {
            "follow_testid": "123-follow",
            "unfollow_testid": None,
            "pending": False,
            "target_mismatch": False,
            "reason": "ok",
        }
        browser._security_interstitial = Mock(return_value=False)
        browser._consume_x_grant = Mock()
        browser._wait_for_initial_content = Mock()
        fallback = ExecutorError(
            "ACTION_UNCERTAIN",
            "unverified",
            details={
                "visual_fallback": {
                    "required": True,
                    "next_tools": [
                        "browser_observe",
                        "browser_screenshot",
                        "browser_click",
                    ],
                    "instruction": "old",
                }
            },
        )
        browser._visual_fallback_error = Mock(return_value=fallback)
        response = Mock(status=200)
        response.request.method = "POST"
        wait = Mock()
        wait.value = response
        browser.page.expect_response.return_value = MagicMock(
            __enter__=Mock(return_value=wait)
        )
        clock = itertools.count(0, 1.0)

        with fake_playwright(), patch(
            "browser_executor.time.monotonic", side_effect=lambda: next(clock)
        ):
            with self.assertRaises(ExecutorError) as caught:
                browser._execute_follow_profile_impl(
                    profile_url="https://x.com/user0", handle="user0"
                )

        details = caught.exception.details
        self.assertTrue(details["write_dispatched"])
        self.assertEqual(details["api_response"]["status"], 200)
        self.assertEqual(
            details["api_response"]["path"],
            "/i/api/1.1/friendships/create.json",
        )
        self.assertIn("attempt_elapsed_seconds", details)
        self.assertEqual(
            details["visual_fallback"]["next_tools"],
            ["browser_observe", "browser_screenshot"],
        )
        self.assertIn(
            "do not click again", details["visual_fallback"]["instruction"]
        )
        browser.page.locator.return_value.click.assert_called_once()
        self.assertGreaterEqual(browser.page.reload.call_count, 1)


if __name__ == "__main__":
    unittest.main()
