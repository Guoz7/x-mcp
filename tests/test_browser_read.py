"""Contract tests for deriving X read results from visible page state."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import browser_read  # noqa: E402
from browser_read import ReadError, read_account, read_timeline  # noqa: E402
from browser_executor import ExecutorError, _visible_articles, _visible_links  # noqa: E402
from server import XStarter  # noqa: E402


LOGIN_WALL = "https://x.com/i/jf/onboarding/web?redirect_after_login=%2Fhome&mode=login"


def _observation(url: str, **overrides) -> dict:
    base = {
        "url": url,
        "title": "X",
        "text": "",
        "width": 1280,
        "height": 720,
        "dpr": 1,
        "locale": "zh-CN",
        "color_scheme": "light",
        "links": [],
        "articles": [],
        "browser_family": "chromium",
    }
    base.update(overrides)
    return base


class VisibleProjectionTests(unittest.TestCase):
    def test_links_drop_queries_fragments_and_foreign_hosts(self) -> None:
        links = _visible_links([
            "https://x.com/guoz7777/status/123?s=20",
            "https://x.com/guoz7777/status/123#frag",
            "https://evil.example/x.com/status/999",
            "javascript:alert(1)",
            "https://x.com/i/flow/login",
        ])
        self.assertEqual(links, [
            "https://x.com/guoz7777/status/123",
            "https://x.com/i/flow/login",
        ])

    def test_links_are_bounded(self) -> None:
        raw = [f"https://x.com/user{i}/status/{i}" for i in range(500)]
        self.assertLessEqual(len(_visible_links(raw)), 60)

    def test_articles_drop_items_without_text(self) -> None:
        articles = _visible_articles([
            {"text": "  ", "links": []},
            {"text": "hello", "links": ["https://x.com/a/status/1"]},
            {"text": None, "links": []},
        ])
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["text"], "hello")

    def test_article_text_is_truncated(self) -> None:
        articles = _visible_articles([{"text": "x" * 5000, "links": []}])
        self.assertEqual(len(articles[0]["text"]), 1500)


class TimelineReadTests(unittest.TestCase):
    def _browser(self, pages: list[dict]) -> Mock:
        browser = Mock()
        browser.open.side_effect = pages
        return browser

    def test_signed_out_page_is_reported_not_guessed(self) -> None:
        browser = self._browser([_observation(LOGIN_WALL)])
        with self.assertRaises(ReadError) as caught:
            read_timeline(browser)
        self.assertEqual(caught.exception.code, "OWNER_SIGNED_OUT")
        self.assertEqual(browser.open.call_count, 1)

    def test_posts_need_a_canonical_permalink(self) -> None:
        browser = self._browser([
            _observation(
                "https://x.com/home",
                articles=[
                    {"text": "no link here", "links": ["https://x.com/i/flow/login"]},
                    {
                        "text": "real post",
                        "links": ["https://x.com/alice/status/555?s=20"],
                    },
                ],
            )
        ])
        result = read_timeline(browser, limit=10)
        self.assertEqual(len(result["posts"]), 1)
        self.assertEqual(result["posts"][0]["url"], "https://x.com/alice/status/555")
        self.assertEqual(result["skipped_without_permalink"], 1)

    def test_empty_timeline_fails_closed(self) -> None:
        browser = self._browser([_observation("https://x.com/home", articles=[])])
        with self.assertRaises(ReadError) as caught:
            read_timeline(browser)
        self.assertEqual(caught.exception.code, "TIMELINE_EMPTY")

    def test_limit_is_respected(self) -> None:
        articles = [
            {"text": f"post {i}", "links": [f"https://x.com/a/status/{i}"]}
            for i in range(10)
        ]
        browser = self._browser([_observation("https://x.com/home", articles=articles)])
        self.assertEqual(len(read_timeline(browser, limit=3)["posts"]), 3)

    def test_executor_error_is_mapped(self) -> None:
        browser = Mock()
        browser.open.side_effect = ExecutorError("BROWSER_NOT_OPEN", "no browser")
        with self.assertRaises(ReadError) as caught:
            read_timeline(browser)
        self.assertEqual(caught.exception.code, "BROWSER_NOT_OPEN")


class AccountReadTests(unittest.TestCase):
    HOME = _observation(
        "https://x.com/home",
        links=["https://x.com/guoz7777", "https://x.com/i/flow/login"],
        articles=[
            {"text": "a post", "links": ["https://x.com/guoz7777/status/2112242496441061458"]}
        ],
    )

    def test_account_read_uses_visible_id_and_profile(self) -> None:
        profile = _observation(
            "https://x.com/guoz7777",
            text="guoz777\n@guoz7777\n5 posts\n芯片架构",
            profile={
                "screen_name": "guoz7777",
                "user_id": "1597769294179627010",
                "recent_post_ids": ["2112242496441061458"],
                "profile_url": "https://x.com/guoz7777",
            },
        )
        browser = Mock()
        browser.open.side_effect = [self.HOME, profile]
        result = read_account(browser)
        self.assertEqual(result["username"], "guoz7777")
        self.assertEqual(result["user_id"], "1597769294179627010")
        self.assertEqual(result["posts_count"], 5)
        self.assertEqual(result["posts_count_source"], "parsed_from_page")
        self.assertEqual(result["display_name"], "guoz777")

    def test_missing_user_id_fails_instead_of_inventing_one(self) -> None:
        profile = _observation(
            "https://x.com/guoz7777",
            text="guoz777\n@guoz7777",
            profile={"screen_name": "guoz7777", "user_id": None},
        )
        browser = Mock()
        browser.open.side_effect = [self.HOME, profile]
        with self.assertRaises(ReadError) as caught:
            read_account(browser)
        self.assertEqual(caught.exception.code, "ACCOUNT_ID_NOT_VISIBLE")

    def test_post_count_falls_back_to_visible_articles(self) -> None:
        profile = _observation(
            "https://x.com/guoz7777",
            text="guoz777\n@guoz7777",
            profile={
                "screen_name": "guoz7777",
                "user_id": "1597769294179627010",
                "recent_post_ids": ["1", "2"],
            },
        )
        browser = Mock()
        browser.open.side_effect = [self.HOME, profile]
        result = read_account(browser)
        self.assertEqual(result["posts_count"], 2)
        self.assertEqual(result["posts_count_source"], "visible_article_count")

    def test_login_wall_on_home_fails_before_any_profile_navigation(self) -> None:
        browser = Mock()
        browser.open.side_effect = [_observation(LOGIN_WALL)]
        with self.assertRaises(ReadError) as caught:
            read_account(browser)
        self.assertEqual(caught.exception.code, "OWNER_SIGNED_OUT")
        self.assertEqual(browser.open.call_count, 1)

    def test_ambiguous_handles_are_rejected(self) -> None:
        home = _observation(
            "https://x.com/home",
            links=["https://x.com/alice", "https://x.com/bob"],
            articles=[],
        )
        browser = Mock()
        browser.open.side_effect = [home]
        with self.assertRaises(ReadError) as caught:
            read_account(browser)
        self.assertEqual(caught.exception.code, "OWNER_HANDLE_AMBIGUOUS")

    def test_author_of_a_visible_post_disambiguates(self) -> None:
        home = _observation(
            "https://x.com/home",
            links=["https://x.com/alice", "https://x.com/bob"],
            articles=[
                {"text": "mine", "links": ["https://x.com/bob/status/777"]},
            ],
        )
        profile = _observation(
            "https://x.com/bob",
            text="Bob\n@bob",
            profile={"screen_name": "bob", "user_id": "123456789", "recent_post_ids": []},
        )
        browser = Mock()
        browser.open.side_effect = [home, profile]
        self.assertEqual(read_account(browser)["username"], "bob")

    def test_localized_post_count_is_parsed(self) -> None:
        profile = _observation(
            "https://x.com/guoz7777",
            text="guoz777\n@guoz7777\n1,234 帖子",
            profile={"screen_name": "guoz7777", "user_id": "1", "recent_post_ids": []},
        )
        browser = Mock()
        browser.open.side_effect = [self.HOME, profile]
        self.assertEqual(read_account(browser)["posts_count"], 1234)

    def test_abbreviated_count_is_labelled_as_an_estimate(self) -> None:
        profile = _observation(
            "https://x.com/guoz7777",
            text="guoz777\n@guoz7777\n12.5K posts",
            profile={"screen_name": "guoz7777", "user_id": "1", "recent_post_ids": []},
        )
        browser = Mock()
        browser.open.side_effect = [self.HOME, profile]
        result = read_account(browser)
        self.assertEqual(result["posts_count"], 12500)
        self.assertEqual(result["posts_count_source"], "abbreviated_estimate")


class OwnerReadToolTests(unittest.TestCase):
    """The MCP tool must report a real browser visit as an external call."""

    def _app(self) -> XStarter:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return XStarter(
            mode="browser",
            environ={
                "X_STARTER_ENABLE_BROWSER": "1",
                "X_BROWSER_EXECUTOR_HEADLESS": "1",
                "X_STARTER_STATE_DIR": str(Path(temp.name) / "state"),
                "X_STARTER_BROWSER_PROFILE_DIR": str(Path(temp.name) / "profiles"),
                "X_BROWSER_EXECUTOR_PROFILE": str(Path(temp.name) / "profile"),
            },
        )

    def test_owner_read_queues_an_action_and_fails_closed_when_signed_out(self) -> None:
        app = self._app()
        browser = Mock()
        with patch.object(app, "_browser_for", return_value=browser), patch.object(
            app, "_bridge"
        ) as bridge_factory:
            from browser_bridge import BrowserBridge
            import tempfile

            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            bridge_factory.return_value = BrowserBridge(temp.name)
            browser.open.side_effect = [_observation(LOGIN_WALL)]
            result = app.call_tool("x_owner_read", {"target": "timeline"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "OWNER_SIGNED_OUT")
        self.assertTrue(result["meta"]["real_external_calls"])
        self.assertFalse(result["meta"]["simulated"])
        # The failed action is still recorded, so the caller can audit it.
        self.assertIn("action_id", result["error"]["details"])

    def test_owner_read_rejects_an_unknown_target(self) -> None:
        app = self._app()
        result = app.call_tool("x_owner_read", {"target": "followers"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    def test_owner_read_requires_browser_mode(self) -> None:
        app = XStarter(mode="mock", environ={})
        result = app.call_tool("x_owner_read", {"target": "timeline"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "BROWSER_BRIDGE_REQUIRED")


if __name__ == "__main__":
    unittest.main()
