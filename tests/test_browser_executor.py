"""Offline contract tests for the browser executor boundary."""

from __future__ import annotations

import os
import sys
import json
import tempfile
import threading
import unittest
import time
from unittest.mock import Mock, patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from browser_executor import (  # noqa: E402
    BROWSER,
    Browser,
    ExecutorError,
    TOOLS,
    _allowed,
    _color_scheme,
    _device_scale_factor,
    _locale,
    _user_agent,
    _viewport,
    _viewport_class,
)
from owner_login_check import classify  # noqa: E402
import owner_login_open  # noqa: E402
from server import SessionRegistry, XStarter, handle_message  # noqa: E402


class BrowserExecutorContractTests(unittest.TestCase):
    def test_login_classifier_prefers_login_wall_url_over_visible_text(self) -> None:
        self.assertEqual(
            classify("https://x.com/i/jf/onboarding/web?redirect_after_login=%2Fhome&mode=login", ""),
            "signed_out",
        )
        self.assertEqual(classify("https://x.com/home", "For you"), "signed_in")
        self.assertEqual(classify("https://x.com/", "使用手机继续"), "signed_out")
        self.assertEqual(classify("https://x.com/", "loading"), "unknown")

    def test_login_opener_refuses_headless_and_missing_profile(self) -> None:
        with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_HEADLESS": "1"}, clear=False):
            os.environ["X_STARTER_OWNER_PROFILE"] = str(ROOT)
            self.assertEqual(owner_login_open.main(), 2)
        with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_HEADLESS": "0"}, clear=False):
            os.environ["X_STARTER_OWNER_PROFILE"] = str(ROOT / "missing-profile-xyz")
            self.assertEqual(owner_login_open.main(), 2)
        with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_HEADLESS": "0"}, clear=False):
            os.environ.pop("X_STARTER_OWNER_PROFILE", None)
            self.assertEqual(owner_login_open.main(), 2)

    def test_default_user_agent_is_not_headless(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("X_BROWSER_EXECUTOR_USER_AGENT", None)
            agent = _user_agent()
        self.assertNotIn("HeadlessChrome", agent)
        self.assertIn("Chrome/", agent)
        with patch.dict("os.environ", {"X_BROWSER_EXECUTOR_USER_AGENT": "test-agent"}):
            self.assertEqual(_user_agent(), "test-agent")

    def test_viewport_class_bands_match_the_bridge_contract(self) -> None:
        # These bands must agree with browser_bridge.VIEWPORT_CLASSES, so the
        # executor never reports a class the bridge would not accept.
        self.assertEqual(_viewport_class(390), "mobile")
        self.assertEqual(_viewport_class(767), "mobile")
        self.assertEqual(_viewport_class(768), "tablet")
        self.assertEqual(_viewport_class(1023), "tablet")
        self.assertEqual(_viewport_class(1024), "desktop")
        self.assertEqual(_viewport_class(1920), "desktop")

    def test_viewport_rejects_values_playwright_would_fail_on(self) -> None:
        with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_WIDTH": "1280",
                                     "X_BROWSER_EXECUTOR_HEIGHT": "720"}, clear=False):
            self.assertEqual(_viewport(), {"width": 1280, "height": 720})
        for bad in ("abc", "0", "10001", "-1"):
            with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_WIDTH": bad}, clear=False):
                with self.assertRaises(ExecutorError) as caught:
                    _viewport()
                self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    def test_device_scale_factor_is_bounded(self) -> None:
        with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_DEVICE_SCALE_FACTOR": "2.5"}):
            self.assertEqual(_device_scale_factor(), 2.5)
        for bad in ("0.4", "8.1", "dpr"):
            with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_DEVICE_SCALE_FACTOR": bad}):
                with self.assertRaises(ExecutorError) as caught:
                    _device_scale_factor()
                self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    def test_locale_is_allowlisted_before_browser_start(self) -> None:
        with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_LOCALE": "zh-CN"}):
            self.assertEqual(_locale(), "zh-CN")
        with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_LOCALE": ""}):
            self.assertEqual(_locale(), "en-US")
        with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_LOCALE": "xx-YY"}):
            with self.assertRaises(ExecutorError) as caught:
                _locale()
            self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    def test_color_scheme_is_optional_and_validated(self) -> None:
        with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_COLOR_SCHEME": ""}):
            self.assertIsNone(_color_scheme())
        with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_COLOR_SCHEME": "DARK"}):
            self.assertEqual(_color_scheme(), "dark")
        with patch.dict(os.environ, {"X_BROWSER_EXECUTOR_COLOR_SCHEME": "sepia"}):
            with self.assertRaises(ExecutorError) as caught:
                _color_scheme()
            self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    def test_login_opener_keeps_window_open_until_owner_is_done(self) -> None:
        """Playwright tears the window down when its driver exits early."""

        with tempfile.TemporaryDirectory() as temp:
            sentinel = Path(temp) / "done"
            browser = Browser()
            with patch.object(owner_login_open, "_page_closed", return_value=False):
                started = time.monotonic()
                threading.Timer(0.6, sentinel.write_text, args=("",)).start()
                reason = owner_login_open.wait_for_owner(
                    browser, wait_seconds=10, sentinel=sentinel
                )
                elapsed = time.monotonic() - started
            self.assertEqual(reason, "sentinel")
            self.assertGreaterEqual(elapsed, 0.5)
            self.assertLess(elapsed, 8)

    def test_login_opener_stops_when_the_owner_closes_the_window(self) -> None:
        browser = Browser()
        with patch.object(owner_login_open, "_page_closed", return_value=True):
            started = time.monotonic()
            reason = owner_login_open.wait_for_owner(browser, wait_seconds=10)
            elapsed = time.monotonic() - started
        self.assertEqual(reason, "window_closed")
        self.assertLess(elapsed, 5)

    def test_login_opener_wait_is_bounded(self) -> None:
        browser = Browser()
        with patch.object(owner_login_open, "_page_closed", return_value=False):
            started = time.monotonic()
            reason = owner_login_open.wait_for_owner(browser, wait_seconds=1.2)
            elapsed = time.monotonic() - started
        self.assertEqual(reason, "timeout")
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertLess(elapsed, 4)

    def test_open_uses_start_timeout_instead_of_interaction_timeout(self) -> None:
        browser = Browser()
        browser.start_timeout = 90
        seen = {}

        def record(function, timeout=30):
            seen["timeout"] = timeout
            return {"ok": True}

        with patch.object(browser, "_run_on_worker", side_effect=record):
            browser.open("https://www.google.com/")
        self.assertEqual(seen["timeout"], 90)

    def test_typing_rejects_empty_and_oversized_text_before_dispatch(self) -> None:
        browser = Browser()
        seen = []
        with patch.object(browser, "_run_on_worker", side_effect=seen.append):
            for value in ("", "x" * 281, None, 123):
                with self.assertRaises(ExecutorError) as caught:
                    browser.type_text(observation_id="obs", fingerprint_sha256="a" * 64, text=value)
                self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(seen, [])

    def test_x_typing_requires_a_claimed_action(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://x.com/home")
        with patch.object(browser, "_assert_observation"):
            with self.assertRaises(ExecutorError) as caught:
                browser._type_impl("obs", "a" * 64, "hello")
        self.assertEqual(caught.exception.code, "X_WRITE_CONFIRMATION_REQUIRED")
        browser.page.keyboard.type.assert_not_called()

    def test_x_typing_consumes_the_grant_once(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://x.com/home")
        browser.authorize_x_action(
            action_id="act", claim_token="t" * 32,
            lease_expires_epoch=time.time() + 60, action_type="publish_post",
        )
        with patch.object(browser, "_assert_observation"), patch.object(
            browser, "_observe_impl", return_value={"observation_id": "fresh"}
        ):
            browser._type_impl("obs", "a" * 64, "hello")
            with self.assertRaises(ExecutorError) as caught:
                browser._type_impl("obs", "a" * 64, "again")
        # The typing stage is single-use. The click stage survives, because
        # publishing has to type the payload and then submit it.
        self.assertEqual(caught.exception.code, "X_ACTION_STAGE_EXHAUSTED")
        browser.page.keyboard.type.assert_called_once_with("hello", delay=0)

    def test_publish_claim_allows_one_type_then_one_click(self) -> None:
        """A publish needs both stages; neither stage may repeat."""

        browser = Browser()
        browser.page = Mock(url="https://x.com/home")
        browser.page.locator.return_value.count.return_value = 1
        browser.authorize_x_action(
            action_id="act", claim_token="t" * 32,
            lease_expires_epoch=time.time() + 60, action_type="publish_post",
        )
        with patch.object(browser, "_assert_observation"), patch.object(
            browser, "_observe_impl", return_value={"observation_id": "fresh"}
        ):
            browser._type_impl("obs", "a" * 64, "hello")
        # Typing must not consume the submit click.
        self.assertIsNotNone(browser.x_grant)
        with patch.object(browser, "_assert_observation"), patch.object(
            browser, "_dispatch_click", return_value={"ok": True}
        ):
            browser._click_impl("obs", "a" * 64, "button", None, None)
        self.assertIsNone(browser.x_grant)

    def test_x_status_selector_scopes_to_the_canonical_article(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://x.com/johnlinkvk37/status/123")
        original = Mock()
        original.count.return_value = 2
        scoped = Mock()
        scoped.count.return_value = 1
        browser.page.locator.side_effect = lambda selector: (
            original if selector == 'article [data-testid="like"]' else scoped
        )
        browser.authorize_x_action(
            action_id="act", claim_token="t" * 32,
            lease_expires_epoch=time.time() + 60, action_type="like_post",
        )
        with patch.object(browser, "_assert_observation"), patch.object(
            browser, "_dispatch_click", return_value={"ok": True}
        ) as dispatch:
            browser._click_impl("obs", "a" * 64, 'article [data-testid="like"]', None, None)
        dispatch.assert_called_once_with(
            'article:has(a[href="/johnlinkvk37/status/123"]) [data-testid="like"]',
            None,
            None,
        )

    def test_scoped_status_selector_waits_for_async_article_mount(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://x.com/johnlinkvk37/status/123")
        scoped = Mock()
        scoped.count.side_effect = [0, 1]
        browser.page.locator.return_value = scoped
        browser.authorize_x_action(
            action_id="act", claim_token="t" * 32,
            lease_expires_epoch=time.time() + 60, action_type="delete_post",
        )
        selector = 'article:has(a[href="/johnlinkvk37/status/123"]) [data-testid="caret"]'
        with patch.object(browser, "_assert_observation"), patch.object(
            browser, "_dispatch_click", return_value={"ok": True}
        ) as dispatch:
            browser._click_impl("obs", "a" * 64, selector, None, None)
        dispatch.assert_called_once_with(selector, None, None)

    def test_x_selector_error_does_not_consume_claim_stage(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://x.com/johnlinkvk37/status/123")
        ambiguous = Mock()
        ambiguous.count.return_value = 2
        browser.page.locator.return_value = ambiguous
        browser.authorize_x_action(
            action_id="act", claim_token="t" * 32,
            lease_expires_epoch=time.time() + 60, action_type="like_post",
        )
        with patch.object(browser, "_assert_observation"):
            with self.assertRaises(ExecutorError) as caught:
                browser._click_impl("obs", "a" * 64, 'article [data-testid="like"]', None, None)
        self.assertEqual(caught.exception.code, "TARGET_NOT_UNIQUE")
        self.assertEqual(browser.x_grant["click_remaining"], 1)

    def test_repost_claim_keeps_one_stage_for_confirmation_click(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://x.com/johnlinkvk37/status/123")
        browser.page.locator.return_value.count.return_value = 1
        browser.authorize_x_action(
            action_id="act", claim_token="t" * 32,
            lease_expires_epoch=time.time() + 60, action_type="repost_post",
        )
        with patch.object(browser, "_assert_observation"), patch.object(
            browser, "_dispatch_click", return_value={"ok": True}
        ) as dispatch:
            browser._click_impl("obs", "a" * 64, 'article [data-testid="retweet"]', None, None)
            browser.authorize_x_action(
                action_id="act", claim_token="t" * 32,
                lease_expires_epoch=time.time() + 60, action_type="repost_post",
            )
            browser._click_impl("obs", "a" * 64, 'article [data-testid="retweet"]', None, None)
        self.assertEqual(dispatch.call_count, 2)
        self.assertIsNone(browser.x_grant)

    def test_second_typing_step_is_rejected_for_the_same_claim(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://x.com/home")
        browser.authorize_x_action(
            action_id="act", claim_token="t" * 32,
            lease_expires_epoch=time.time() + 60, action_type="publish_post",
        )
        with patch.object(browser, "_assert_observation"), patch.object(
            browser, "_observe_impl", return_value={"observation_id": "fresh"}
        ):
            browser._type_impl("obs", "a" * 64, "first")
            with self.assertRaises(ExecutorError) as caught:
                browser._type_impl("obs", "a" * 64, "second")
        self.assertEqual(caught.exception.code, "X_ACTION_STAGE_EXHAUSTED")
        # Exhausting typing must not disarm the pending submit click.
        self.assertIsNotNone(browser.x_grant)
        self.assertEqual(browser.x_grant["click_remaining"], 1)

    def test_delete_claim_cannot_type(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://x.com/home")
        browser.authorize_x_action(
            action_id="act", claim_token="t" * 32,
            lease_expires_epoch=time.time() + 60, action_type="delete_post",
        )
        with patch.object(browser, "_assert_observation"):
            with self.assertRaises(ExecutorError) as caught:
                browser._type_impl("obs", "a" * 64, "nope")
        self.assertEqual(caught.exception.code, "X_ACTION_SCOPE_MISMATCH")

    def test_reauthorizing_the_same_claim_cannot_refill_a_stage(self) -> None:
        """A second tool call must not reset the counters for one claim."""

        browser = Browser()
        browser.page = Mock(url="https://x.com/home")
        browser.authorize_x_action(
            action_id="act", claim_token="t" * 32,
            lease_expires_epoch=time.time() + 60, action_type="publish_post",
        )
        with patch.object(browser, "_assert_observation"), patch.object(
            browser, "_observe_impl", return_value={"observation_id": "fresh"}
        ):
            browser._type_impl("obs", "a" * 64, "hello")
        # The server re-verifies the claim on every browser tool call.
        browser.authorize_x_action(
            action_id="act", claim_token="t" * 32,
            lease_expires_epoch=time.time() + 60, action_type="publish_post",
        )
        with patch.object(browser, "_assert_observation"):
            with self.assertRaises(ExecutorError) as caught:
                browser._type_impl("obs", "a" * 64, "again")
        self.assertEqual(caught.exception.code, "X_ACTION_STAGE_EXHAUSTED")
        # The submit click is still available exactly once.
        self.assertEqual(browser.x_grant["click_remaining"], 1)

    def test_fully_used_claim_cannot_be_rearmed(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://x.com/home")
        browser.page.locator.return_value.count.return_value = 1
        browser.authorize_x_action(
            action_id="act", claim_token="t" * 32,
            lease_expires_epoch=time.time() + 60, action_type="delete_post",
        )
        with patch.object(browser, "_assert_observation"), patch.object(
            browser, "_dispatch_click", return_value={"ok": True}
        ):
            browser._click_impl("obs", "a" * 64, "button", None, None)
        with self.assertRaises(ExecutorError) as caught:
            browser.authorize_x_action(
                action_id="act", claim_token="t" * 32,
                lease_expires_epoch=time.time() + 60, action_type="delete_post",
            )
        self.assertEqual(caught.exception.code, "X_ACTION_EXHAUSTED")

    def test_non_x_typing_does_not_need_a_claim(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://www.google.com/")
        with patch.object(browser, "_assert_observation"), patch.object(
            browser, "_observe_impl", return_value={"observation_id": "fresh"}
        ):
            browser._type_impl("obs", "a" * 64, "query")
        browser.page.keyboard.type.assert_called_once()

    def test_server_type_tool_shares_the_same_claim_gate_as_click(self) -> None:
        app = XStarter(mode="browser", environ={"X_STARTER_ENABLE_BROWSER": "1"})
        app._bridge().record_account(
            username="owner", user_id="1", display_name="Owner",
            posts_count=3, recent_post_ids=[],
        )
        record = app._bridge().prepare_publish(
            text="hello", expected_username="owner", marker=None,
            ttl_seconds=180, session_id="s1",
        )
        app._bridge().queue_write(
            record["write_id"], record["confirmation_phrase"], session_id="s1"
        )
        claimed = app._bridge().next_action(lease_seconds=60, session_id="s1")
        args = {
            "observation_id": "obs", "fingerprint_sha256": "a" * 64,
            "text": "hello", "action_id": claimed["action_id"],
            "claim_token": claimed["claim_token"],
        }
        with patch.object(app, "_browser_for") as browser_for:
            browser_for.return_value.type_text.return_value = {"observation_id": "new"}
            result = app.call_tool("browser_type", args, session_id="s1")
            self.assertTrue(result["ok"])
            browser_for.return_value.authorize_x_action.assert_called_once()
            browser_for.return_value.type_text.assert_called_once()
        # A half-supplied claim is refused before touching the browser.
        with patch.object(app, "_browser_for") as browser_for:
            bad = dict(args)
            del bad["claim_token"]
            result = app.call_tool("browser_type", bad, session_id="s1")
            self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
            browser_for.return_value.type_text.assert_not_called()
        app.close()

    def test_timeout_marks_executor_as_stuck(self) -> None:
        browser = Browser()
        with patch.object(browser, "_ensure_worker"), patch.object(
            browser.tasks, "put"
        ), patch("browser_executor.threading.Event.wait", return_value=False):
            with self.assertRaises(ExecutorError):
                browser._run_on_worker(lambda: None, timeout=0.01)
        self.assertTrue(browser.timed_out)

    def test_scroll_bounds_reject_before_worker_dispatch(self) -> None:
        browser = Browser()
        with patch.object(browser, "_run_on_worker") as dispatch:
            for value in (True, 2001, -2001, "100", None, 1.5):
                with self.assertRaises(ExecutorError) as caught:
                    browser.scroll(observation_id="obs", fingerprint_sha256="a" * 64, delta_y=value)
                self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
            dispatch.assert_not_called()

    def test_scroll_on_x_does_not_click_or_type(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://x.com/home")
        browser.visual_observation_id = "obs"
        with patch.object(browser, "_assert_observation"), patch.object(
            browser, "_observe_impl", return_value={"observation_id": "fresh"}
        ):
            self.assertEqual(browser._scroll_impl("obs", "a" * 64, 500)["observation_id"], "fresh")
        browser.page.evaluate.assert_called_once_with("delta => window.scrollBy({top: delta, left: 0, behavior: 'instant'})", 500)
        browser.page.locator.assert_not_called()
        browser.page.mouse.click.assert_not_called()
        browser.page.keyboard.press.assert_not_called()
        self.assertIsNone(browser.visual_observation_id)

    def test_scroll_error_consumes_observation(self) -> None:
        browser = Browser()
        browser.page = Mock()
        browser.page.evaluate.side_effect = TimeoutError
        browser.observation_id = "obs"
        with patch.object(browser, "_assert_observation"):
            with self.assertRaises(ExecutorError) as caught:
                browser._scroll_impl("obs", "a" * 64, 500)
        self.assertEqual(caught.exception.code, "ACTION_UNCERTAIN")
        self.assertIsNone(browser.observation_id)

    def test_mcp_sessions_have_distinct_executors_and_profiles(self) -> None:
        app = XStarter(mode="browser", environ={"X_STARTER_ENABLE_BROWSER": "1"})
        first = app._browser_for({"_mcp_session_id": "first"})
        second = app._browser_for({"_mcp_session_id": "second"})
        self.assertIsNot(first, second)
        self.assertNotEqual(first.profile, second.profile)
        first.observation_id = "private-observation"
        with self.assertRaises(ExecutorError):
            second._assert_observation("private-observation", "a" * 64)
        app.close()
        self.assertEqual(app._browsers, {})
        result = app.call_tool("browser_open", {"url": "https://x.com/"}, session_id="after-shutdown")
        self.assertEqual(result["error"]["code"], "BROWSER_EXECUTOR_CLOSED")

    def test_every_browser_tool_dispatches_with_mcp_session(self) -> None:
        app = XStarter(mode="browser", environ={"X_STARTER_ENABLE_BROWSER": "1"})
        with patch("server.Browser") as factory:
            factory.return_value.open.return_value = {"ok": True}
            factory.return_value.observe.return_value = {"observation_id": "obs"}
            factory.return_value.scroll.return_value = {"observation_id": "scrolled"}
            for session in ("first", "second"):
                self.assertTrue(app.call_tool("browser_open", {"url": "https://x.com/"}, session_id=session)["ok"])
                self.assertTrue(app.call_tool("browser_observe", {}, session_id=session)["ok"])
                self.assertTrue(app.call_tool("browser_scroll", {
                    "observation_id": "obs", "fingerprint_sha256": "a" * 64, "delta_y": 500,
                }, session_id=session)["ok"])
                self.assertTrue(app.call_tool("browser_close", {}, session_id=session)["ok"])
            self.assertEqual(factory.call_count, 2)
            app.close_browser_session("first")
            self.assertNotIn("first", app._browsers)
            self.assertIn("second", app._browsers)
            app.close()

    def test_executor_capacity_is_bounded(self) -> None:
        app = XStarter(mode="browser", environ={"X_STARTER_ENABLE_BROWSER": "1"})
        for index in range(8):
            app._browser_for({"_mcp_session_id": str(index)})
        result = app.call_tool("browser_open", {"url": "https://x.com/"}, session_id="overflow")
        self.assertEqual(result["error"]["code"], "BROWSER_CAPACITY")
        app.close()

    def test_stuck_executor_is_replaced_on_next_call(self) -> None:
        app = XStarter(mode="browser", environ={"X_STARTER_ENABLE_BROWSER": "1"})
        stuck = app._browser_for({"_mcp_session_id": "session"})
        stuck.timed_out = True
        with patch.object(stuck, "close") as close:
            fresh = app._browser_for({"_mcp_session_id": "session"})
        close.assert_called_once()
        self.assertIsNot(fresh, stuck)
        self.assertIs(app._browsers["session"], fresh)
        app.close()

    def test_owner_profile_is_exclusive_to_one_session(self) -> None:
        with tempfile.TemporaryDirectory() as profile:
            app = XStarter(mode="browser", environ={
                "X_STARTER_ENABLE_BROWSER": "1", "X_STARTER_OWNER_PROFILE": profile,
            })
            first = app._browser_for({"_mcp_session_id": "owner"})
            self.assertEqual(str(first.profile), str(Path(profile).resolve()))
            result = app.call_tool("browser_open", {"url": "https://x.com/"}, session_id="other")
            self.assertEqual(result["error"]["code"], "BROWSER_OWNER_EXCLUSIVE")
            app.close_browser_session("owner")
            replacement = app._browser_for({"_mcp_session_id": "other2"})
            self.assertIsNot(replacement, first)
            app.close()

    def test_disposable_profiles_avoid_the_state_disk_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            app = XStarter(mode="browser", environ={
                "X_STARTER_ENABLE_BROWSER": "1",
                "X_STARTER_STATE_DIR": state,
                "LOCALAPPDATA": r"C:\Users\test\AppData\Local",
            })
            browser = app._browser_for({"_mcp_session_id": "s"})
            self.assertNotIn(state, str(browser.profile))
            self.assertIn("x-starter", str(browser.profile))
            app.close()
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as profiles:
            app = XStarter(mode="browser", environ={
                "X_STARTER_ENABLE_BROWSER": "1",
                "X_STARTER_STATE_DIR": state,
                "X_STARTER_BROWSER_PROFILE_DIR": profiles,
            })
            browser = app._browser_for({"_mcp_session_id": "s"})
            self.assertTrue(str(browser.profile).startswith(str(Path(profiles).resolve())))
            app.close()

    def test_default_state_dir_avoids_removable_media(self) -> None:
        """Every action fsyncs state; a USB HDD made that 29 s per cycle."""

        local = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(local, ignore_errors=True))
        app = XStarter(
            mode="browser",
            environ={"X_STARTER_ENABLE_BROWSER": "1", "LOCALAPPDATA": local},
        )
        try:
            state = str(app.bridge.root)
            self.assertTrue(state.startswith(str(Path(local).resolve())), state)
            self.assertNotIn(str(ROOT), state)
        finally:
            app.close()

    def test_owner_profile_does_not_embed_credentials_in_tool_output(self) -> None:
        with tempfile.TemporaryDirectory() as profile:
            app = XStarter(mode="browser", environ={
                "X_STARTER_ENABLE_BROWSER": "1", "X_STARTER_OWNER_PROFILE": profile,
            })
            browser = app._browser_for({"_mcp_session_id": "owner"})
            result = app.call_tool("browser_observe", {}, session_id="owner")
            self.assertNotIn(profile, json.dumps(result))
            self.assertFalse(hasattr(browser, "cookies"))
            app.close()

    def test_session_expiry_closes_the_browser(self) -> None:
        app = XStarter(mode="browser", environ={"X_STARTER_ENABLE_BROWSER": "1"})
        registry = SessionRegistry(idle_seconds=-1, on_expire=app.close_browser_session)
        session = registry.create("2025-11-25")
        browser = app._browser_for({"_mcp_session_id": session.session_id})
        with patch.object(browser, "close") as close:
            # The capacity probe is a public entry that sweeps expired sessions.
            registry.has_capacity()
            self.assertNotIn(session.session_id, registry._sessions)
        close.assert_called_once()
        self.assertEqual(app._browsers, {})
        app.close()

    def test_coordinate_requires_current_observation_screenshot(self) -> None:
        browser = Browser()
        with self.assertRaises(ExecutorError) as caught:
            browser._assert_visual_snapshot("obs")
        self.assertEqual(caught.exception.code, "SCREENSHOT_REQUIRED")

    def test_pixel_change_invalidates_coordinate_even_without_scroll(self) -> None:
        browser = Browser()
        browser.observation_id = "obs"
        browser.visual_observation_id = "obs"
        browser.visual_digest = "a" * 64
        browser.page = Mock()
        browser.page.screenshot.return_value = b"changed pixels"
        with self.assertRaises(ExecutorError) as caught:
            browser._assert_visual_snapshot("obs")
        self.assertEqual(caught.exception.code, "OBSERVATION_STALE")
        self.assertIsNone(browser.observation_id)

    def test_screenshot_uses_mcp_image_block_not_base64_text(self) -> None:
        app = XStarter(mode="mock", environ={})
        fake_result = {"ok": True, "data": {"mime_type": "image/png", "data_base64": "aW1hZ2U="}}
        with patch.object(app, "call_tool", return_value=fake_result):
            response = handle_message(app, {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "browser_screenshot", "arguments": {"include_data": True}},
            })["result"]
        self.assertEqual(response["content"][1], {
            "type": "image", "mimeType": "image/png", "data": "aW1hZ2U=",
        })
        self.assertNotIn("data_base64", response["structuredContent"]["data"])
        self.assertNotIn("aW1hZ2U=", response["content"][0]["text"])

    def test_mock_mode_never_dispatches_real_browser_tools(self) -> None:
        app = XStarter(mode="mock", environ={})
        arguments = {"observation_id": "obs", "fingerprint_sha256": "a" * 64}
        with patch("server.Browser") as executor:
            for tool in ("browser_screenshot", "browser_click", "browser_scroll", "browser_close"):
                result = app.call_tool(tool, arguments if tool != "browser_close" else {})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "BROWSER_BRIDGE_REQUIRED")
            self.assertEqual(executor.mock_calls, [])

    def test_server_click_requires_matching_claim_token(self) -> None:
        app = XStarter(mode="browser", environ={"X_STARTER_ENABLE_BROWSER": "1"})
        app._bridge().record_account(
            username="owner", user_id="1", display_name="Owner",
            posts_count=3, recent_post_ids=[],
        )
        record = app._bridge().prepare_publish(
            text="hello", expected_username="owner", marker=None,
            ttl_seconds=180, session_id="s1",
        )
        app._bridge().queue_write(
            record["write_id"], record["confirmation_phrase"], session_id="s1"
        )
        claimed = app._bridge().next_action(lease_seconds=60, session_id="s1")
        args = {
            "observation_id": "obs", "fingerprint_sha256": "a" * 64,
            "selector": "button", "action_id": claimed["action_id"],
            "claim_token": "wrong" * 8,
        }
        result = app.call_tool("browser_click", args, session_id="s1")
        self.assertEqual(result["error"]["code"], "CLAIM_TOKEN_MISMATCH")
        # A valid claim arms the executor before dispatch.
        with patch.object(app, "_browser_for") as browser_for:
            browser_for.return_value.click.return_value = {"observation_id": "new"}
            args["claim_token"] = claimed["claim_token"]
            result = app.call_tool("browser_click", args, session_id="s1")
            self.assertTrue(result["ok"])
            browser_for.return_value.authorize_x_action.assert_called_once()
        app.close()

    def test_cross_session_claim_cannot_be_used(self) -> None:
        app = XStarter(mode="browser", environ={"X_STARTER_ENABLE_BROWSER": "1"})
        app._bridge().record_account(
            username="owner", user_id="1", display_name="Owner",
            posts_count=3, recent_post_ids=[],
        )
        record = app._bridge().prepare_publish(
            text="hello", expected_username="owner", marker=None,
            ttl_seconds=180, session_id="owner",
        )
        app._bridge().queue_write(
            record["write_id"], record["confirmation_phrase"], session_id="owner"
        )
        claimed = app._bridge().next_action(lease_seconds=60, session_id="owner")
        result = app.call_tool("browser_click", {
            "observation_id": "obs", "fingerprint_sha256": "a" * 64, "selector": "button",
            "action_id": claimed["action_id"], "claim_token": claimed["claim_token"],
        }, session_id="intruder")
        self.assertEqual(result["error"]["code"], "ACTION_NOT_FOUND")
        app.close()

    def test_read_only_observation_claim_cannot_authorize_a_write(self) -> None:
        app = XStarter(mode="browser", environ={"X_STARTER_ENABLE_BROWSER": "1"})
        app._bridge().queue_observation(action_type="observe_account", session_id="s1")
        claimed = app._bridge().next_action(lease_seconds=60, session_id="s1")
        with patch.object(app, "_browser_for") as browser_for:
            browser_for.return_value.authorize_x_action.side_effect = ExecutorError(
                "X_ACTION_NOT_A_WRITE", "Only a write claim may touch X."
            )
            result = app.call_tool("browser_click", {
                "observation_id": "obs", "fingerprint_sha256": "a" * 64,
                "selector": "button", "action_id": claimed["action_id"],
                "claim_token": claimed["claim_token"],
            }, session_id="s1")
            self.assertEqual(result["error"]["code"], "X_ACTION_NOT_A_WRITE")
            browser_for.return_value.click.assert_not_called()
        app.close()

    def test_read_claim_cannot_type_on_x_either(self) -> None:
        app = XStarter(mode="browser", environ={"X_STARTER_ENABLE_BROWSER": "1"})
        browser = Browser()
        browser.page = Mock(url="https://x.com/home")
        with self.assertRaises(ExecutorError) as caught:
            browser.authorize_x_action(
                action_id="act", claim_token="t" * 32,
                lease_expires_epoch=time.time() + 60, action_type="observe_timeline",
            )
        self.assertEqual(caught.exception.code, "X_ACTION_NOT_A_WRITE")
        self.assertIsNone(browser.x_grant)

    def test_health_reports_executor_state_not_just_ok(self) -> None:
        app = XStarter(mode="browser", environ={"X_STARTER_ENABLE_BROWSER": "1"})
        browser = app._browser_for({"_mcp_session_id": "s1"})
        browser.timed_out = True
        with app._browser_lock:
            self.assertTrue(getattr(app._browsers["s1"], "timed_out"))
        app.close()

    def test_allowlist_rejects_unrelated_hosts(self) -> None:
        self.assertTrue(_allowed("https://www.google.com/"))
        self.assertTrue(_allowed("https://x.com/home"))
        self.assertFalse(_allowed("https://example.org/"))

    def test_tool_catalog_contains_visual_fallback_tools(self) -> None:
        names = {tool["name"] for tool in TOOLS}
        self.assertTrue({"browser_open", "browser_observe", "browser_screenshot", "browser_click"} <= names)

    def test_stale_observation_is_rejected_before_click(self) -> None:
        BROWSER.observation_id = "obs-current"
        BROWSER.fingerprint = "a" * 64
        with self.assertRaises(ExecutorError) as caught:
            BROWSER._assert_observation("obs-old", "b" * 64)
        self.assertEqual(caught.exception.code, "OBSERVATION_STALE")

    def test_live_state_change_rejects_cached_matching_fingerprint(self) -> None:
        browser = Browser()
        browser.observation_id = "obs-current"
        browser.fingerprint = "a" * 64
        browser.observed_at = time.monotonic()
        with patch.object(browser, "_observe_impl", return_value={"fingerprint_sha256": "b" * 64}):
            with self.assertRaises(ExecutorError) as caught:
                browser._assert_observation("obs-current", "a" * 64)
        self.assertEqual(caught.exception.code, "OBSERVATION_STALE")

    def test_expired_observation_is_invalidated(self) -> None:
        browser = Browser()
        browser.observation_id = "obs-current"
        browser.fingerprint = "a" * 64
        browser.observed_at = time.monotonic() - 121
        with self.assertRaises(ExecutorError):
            browser._assert_observation("obs-current", "a" * 64)
        self.assertIsNone(browser.observation_id)

    def test_generic_x_click_cannot_bypass_write_confirmation(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://x.com/home")
        with patch.object(browser, "_assert_observation"):
            with self.assertRaises(ExecutorError) as caught:
                browser._click_impl("obs", "a" * 64, "button", None, None)
        self.assertEqual(caught.exception.code, "X_WRITE_CONFIRMATION_REQUIRED")
        browser.page.locator.assert_not_called()

    def test_claimed_action_authorizes_exactly_one_x_click(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://x.com/home")
        browser.page.locator.return_value.count.return_value = 1
        browser.authorize_x_action(
            action_id="act_1", claim_token="t" * 32,
            lease_expires_epoch=time.time() + 60, action_type="publish_post",
        )
        with patch.object(browser, "_assert_observation"), patch.object(
            browser, "_dispatch_click", return_value={"ok": True}
        ):
            browser._click_impl("obs", "a" * 64, "button", None, None)
        self.assertIsNone(browser.x_grant)
        with patch.object(browser, "_assert_observation"):
            with self.assertRaises(ExecutorError) as caught:
                browser._click_impl("obs", "a" * 64, "button", None, None)
        self.assertEqual(caught.exception.code, "X_WRITE_CONFIRMATION_REQUIRED")

    def test_expired_or_missing_claim_does_not_grant_x_access(self) -> None:
        browser = Browser()
        with self.assertRaises(ExecutorError) as caught:
            browser.authorize_x_action(
                action_id="act_1", claim_token="t" * 32,
                lease_expires_epoch=time.time() - 1, action_type="publish_post",
            )
        self.assertEqual(caught.exception.code, "ACTION_LEASE_EXPIRED")
        browser.page = Mock(url="https://x.com/home")
        with patch.object(browser, "_assert_observation"):
            with self.assertRaises(ExecutorError):
                browser._click_impl("obs", "a" * 64, "button", None, None)
        browser.page.locator.assert_not_called()

    def test_ambiguous_selector_is_not_clicked(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://www.google.com/")
        browser.page.locator.return_value.count.return_value = 2
        with patch.object(browser, "_assert_observation"):
            with self.assertRaises(ExecutorError) as caught:
                browser._click_impl("obs", "a" * 64, "a", None, None)
        self.assertEqual(caught.exception.code, "TARGET_NOT_UNIQUE")
        browser.page.locator.return_value.click.assert_not_called()

    def test_dispatch_timeout_consumes_observation_and_reports_uncertain(self) -> None:
        browser = Browser()
        browser.page = Mock(url="https://www.google.com/")
        browser.observation_id = "obs"
        browser.page.locator.return_value.count.return_value = 1
        with patch.object(browser, "_assert_observation"), patch.object(
            browser, "_dispatch_click", side_effect=TimeoutError
        ):
            with self.assertRaises(ExecutorError) as caught:
                browser._click_impl("obs", "a" * 64, "a", None, None)
        self.assertEqual(caught.exception.code, "ACTION_UNCERTAIN")
        self.assertIsNone(browser.observation_id)


if __name__ == "__main__":
    unittest.main()

class BatchFastPathTests(unittest.TestCase):
    def test_fast_path_uses_single_worker_call(self):
        browser = Browser()
        with patch.object(browser, "_run_on_worker", return_value={"verified": True}) as run:
            result = browser.execute_x_action(
                action_type="like_post",
                post_url="https://x.com/a/status/1",
                author="a",
                text=None,
                action_id="act",
                claim_token="t" * 32,
                lease_expires_epoch=time.time() + 60,
            )
        self.assertEqual(result, {"verified": True})
        run.assert_called_once()
