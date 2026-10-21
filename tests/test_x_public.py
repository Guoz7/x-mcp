"""Offline tests for the credential-free public read client.

The fixtures below are the shapes X actually returns: profile data arrives as
double-escaped Relay fragments, the embed timeline arrives as Next.js JSON, and
the single-post endpoint arrives as plain JSON.

These tests never open a socket.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import XStarter  # noqa: E402
from x_public import (  # noqa: E402
    PublicReadError,
    PublicXClient,
    extract_profile,
    extract_timeline,
)


PROFILE_HTML = r"""<html><script>{"user":{"__typename":"User","avatar":{"image_url":"https://pbs.twimg.com/profile_images/1/abc_normal.jpg"},"core":{"created_at_ms":1669772869168,"name":"guoz777","screen_name":"guoz7777"},"privacy":{"protected":false},"relationship_counts":{"followers":5,"following":62},"rest_id":"1597769294179627010","subscribers_count":0,"tweet_counts":{"tweets":5},"verification":{"is_blue_verified":false,"verified":false}}}</script>
<script>{"profiles":{"description":"985本硕 芯片架构方向\n\n中转站地址 https://t.co/x","entities":{}}}</script>
<script>{"tweets":{"result":{"rest_id":"2112242496441061458","result":{"__isTweetResult":"Tweet","core":{"user_results":{"result":{"__typename":"User","core":{"name":"guoz777","screen_name":"guoz7777"}}}},"details":{"created_at_ms":1792432829000,"full_text":"hello\n\nworld","hashtag_entities":[]},"counts":{"bookmark_count":0,"favorite_count":3,"quote_count":0,"reply_count":1,"retweet_count":2}}}}}</script>
</html>"""

ESCAPED_PROFILE_HTML = PROFILE_HTML.replace('"', '\\"')

TIMELINE_HTML = (
    '<html><script id="__NEXT_DATA__" type="application/json">'
    + json.dumps(
        {
            "props": {
                "pageProps": {
                    "latest_tweet_id": "2112242496441061458",
                    "timeline": {
                        "entries": [
                            {
                                "type": "tweet",
                                "content": {
                                    "tweet": {
                                        "id_str": "2112242496441061458",
                                        "permalink": "/guoz7777/status/2112242496441061458",
                                        "created_at": "Mon Oct 19 18:00:29 +0000 2026",
                                        "full_text": "first",
                                        "favorite_count": 4,
                                        "reply_count": 1,
                                        "retweet_count": 2,
                                        "conversation_id_str": "2112242496441061458",
                                        "user": {"screen_name": "guoz7777", "name": "guoz777"},
                                    }
                                },
                            },
                            {
                                "type": "tweet",
                                "content": {
                                    "tweet": {
                                        "id_str": "2110000000000000001",
                                        "permalink": "/guoz7777/status/2110000000000000001",
                                        "created_at": "Sun Oct 18 10:00:00 +0000 2026",
                                        "full_text": "second",
                                        "user": {"screen_name": "guoz7777"},
                                    }
                                },
                            },
                        ]
                    },
                }
            }
        }
    )
    + "</script></html>"
)

POST_JSON = json.dumps(
    {
        "id_str": "2112242496441061458",
        "text": "hello world",
        "created_at": "2026-10-19T18:00:29.000Z",
        "lang": "zh",
        "favorite_count": 7,
        "conversation_count": 2,
        "user": {"screen_name": "guoz7777", "name": "guoz777"},
        "entities": {"urls": [], "media": [{"media_url_https": "https://pbs.twimg.com/x.jpg"}]},
    }
)


class ProfileParseTests(unittest.TestCase):
    def test_parses_escaped_and_plain_relay_fragments(self) -> None:
        for html in (PROFILE_HTML, ESCAPED_PROFILE_HTML):
            profile = extract_profile(html, "guoz7777")
            self.assertEqual(profile["screen_name"], "guoz7777")
            self.assertEqual(profile["user_id"], "1597769294179627010")
            self.assertEqual(profile["metrics"]["followers"], 5)
            self.assertEqual(profile["metrics"]["following"], 62)
            self.assertEqual(profile["metrics"]["posts"], 5)
            self.assertFalse(profile["protected"])

    def test_accepts_at_sign_and_url(self) -> None:
        for value in ("@guoz7777", "guoz7777", "https://x.com/guoz7777"):
            profile = extract_profile(PROFILE_HTML, value)
            self.assertEqual(profile["screen_name"], "guoz7777")

    def test_rejects_a_non_profile_url(self) -> None:
        with self.assertRaises(PublicReadError) as caught:
            extract_profile(PROFILE_HTML, "https://example.com/guoz7777")
        self.assertEqual(caught.exception.code, "INVALID_SCREEN_NAME")

    def test_unparseable_page_raises_instead_of_inventing_values(self) -> None:
        with self.assertRaises(PublicReadError) as caught:
            extract_profile("<html>rate limited</html>", "guoz7777")
        self.assertEqual(caught.exception.code, "PROFILE_NOT_PARSED")


class TimelineParseTests(unittest.TestCase):
    def test_reads_embed_entries_in_order_and_respects_limit(self) -> None:
        posts = extract_timeline(TIMELINE_HTML, "guoz7777", 2)
        self.assertEqual([post["id"] for post in posts], ["2112242496441061458", "2110000000000000001"])
        self.assertEqual(posts[0]["metrics"]["likes"], 4)
        self.assertEqual(posts[0]["created_at"], "2026-10-19T18:00:29+00:00")
        self.assertEqual(len(extract_timeline(TIMELINE_HTML, "guoz7777", 1)), 1)

    def test_empty_embed_page_falls_back_to_newest_profile_post(self) -> None:
        empty = (
            '<html><script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"latest_tweet_id":"2112242496441061458",'
            '"timeline":{"entries":[]}}}}</script></html>'
        )
        posts = extract_timeline(empty, "guoz7777", 5)
        self.assertEqual(posts, [])
        # The client makes two separate requests; the profile page is the fallback.
        client = PublicXClient(
            transport=lambda url, headers, timeout: (
                (200, PROFILE_HTML.encode()) if "x.com/guoz7777" in url else (200, empty.encode())
            ),
            min_interval=0,
        )
        posts = client.get_timeline("guoz7777", 5)["posts"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["id"], "2112242496441061458")
        self.assertEqual(posts[0]["text"], "hello\n\nworld")
        self.assertEqual(posts[0]["metrics"]["likes"], 3)


class ClientTests(unittest.TestCase):
    def _client(self, routes: dict[str, tuple[int, bytes]]) -> PublicXClient:
        calls: list[str] = []

        def transport(url: str, headers: dict[str, str], timeout: float):
            calls.append(url)
            for fragment, response in routes.items():
                if fragment in url:
                    return response
            return 404, b""

        client = PublicXClient(transport=transport, min_interval=0, cache_ttl=60)
        client.calls = calls  # type: ignore[attr-defined]
        return client

    def test_profile_and_timeline_are_cached(self) -> None:
        client = self._client(
            {
                "x.com/guoz7777": (200, PROFILE_HTML.encode()),
                "syndication": (200, TIMELINE_HTML.encode()),
            }
        )
        first = client.get_profile("guoz7777")
        second = client.get_profile("guoz7777")
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(len(client.calls), 1)  # type: ignore[attr-defined]
        self.assertEqual(first["credential"], "none")
        self.assertEqual(client.stats["cache_hits"], 1)

    def test_post_read_maps_counts_and_media(self) -> None:
        client = self._client({"tweet-result": (200, POST_JSON.encode())})
        result = client.get_post("https://x.com/guoz7777/status/2112242496441061458")
        self.assertEqual(result["post"]["id"], "2112242496441061458")
        self.assertEqual(result["post"]["metrics"]["likes"], 7)
        self.assertEqual(result["post"]["media_count"], 1)
        self.assertEqual(result["post"]["created_at"], "2026-10-19T18:00:29Z")

    def test_missing_post_reports_not_found(self) -> None:
        client = self._client({"tweet-result": (404, b"")})
        with self.assertRaises(PublicReadError) as caught:
            client.get_post("2112242496441061458")
        self.assertEqual(caught.exception.code, "PUBLIC_READ_HTTP")
        self.assertEqual(caught.exception.status, 404)

    def test_rate_limit_is_retried_then_reported(self) -> None:
        client = self._client({"x.com/guoz7777": (429, b"slow down")})
        with self.assertRaises(PublicReadError) as caught:
            client.get_profile("guoz7777")
        self.assertEqual(caught.exception.code, "PUBLIC_READ_RATE_LIMITED")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(len(client.calls), 3)  # type: ignore[attr-defined]


class ServerToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = XStarter(mode="mock", environ={})
        self.app.x_public = PublicXClient(
            transport=lambda url, headers, timeout: (200, (
                TIMELINE_HTML.encode() if "syndication" in url else PROFILE_HTML.encode()
            )),
            min_interval=0,
        )

    def test_public_tools_are_declared_read_only_and_noauth(self) -> None:
        tools = {tool["name"]: tool for tool in self.app.tool_definitions()}
        for name in ("x_public_profile", "x_public_timeline", "x_public_post"):
            self.assertIn(name, tools)
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
            self.assertEqual(tools[name]["securitySchemes"], [{"type": "noauth"}])

    def test_tool_result_reports_a_real_external_read(self) -> None:
        result = self.app.call_tool("x_public_profile", {"screen_name": "@guoz7777"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["meta"]["real_external_calls"])
        self.assertFalse(result["meta"]["simulated"])
        self.assertEqual(result["data"]["profile"]["metrics"]["followers"], 5)
        self.assertEqual(result["data"]["source"], "x_public_web")

    def test_public_timeline_limit_is_validated(self) -> None:
        result = self.app.call_tool("x_public_timeline", {"screen_name": "guoz7777", "limit": 0})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    def test_protected_account_returns_a_labelled_empty_list(self) -> None:
        empty = (
            '<html><script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"latest_tweet_id":"1","timeline":{"entries":[]}}}}'
            "</script></html>"
        )
        self.app.x_public = PublicXClient(
            transport=lambda url, headers, timeout: (200, empty.encode()),
            min_interval=0,
        )
        result = self.app.call_tool("x_public_timeline", {"screen_name": "someone"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["posts"], [])

    def test_public_tools_need_no_oauth_and_no_api_key(self) -> None:
        """The zero-setup promise is a contract: no credential field anywhere."""

        tools = {tool["name"]: tool for tool in self.app.tool_definitions()}
        for name in ("x_public_profile", "x_public_timeline", "x_public_post"):
            schema = json.dumps(tools[name]["inputSchema"]).lower()
            for word in ("token", "secret", "password", "authorization", "cookie"):
                self.assertNotIn(word, schema)
        session = self.app.call_tool("x_session_status")
        self.assertTrue(session["ok"])
        self.assertEqual(session["data"]["mode"], "mock")

    def test_read_only_call_leaves_no_write_record(self) -> None:
        before = len(self.app.api_writes.audit(limit=200)["events"])
        self.app.call_tool("x_public_profile", {"screen_name": "guoz7777"})
        after = len(self.app.api_writes.audit(limit=200)["events"])
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
