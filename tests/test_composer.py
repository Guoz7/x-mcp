"""Tests for the Web Intent composer engine."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import build_insights  # noqa: E402
import composer  # noqa: E402
from composer import ComposeError  # noqa: E402

INSIGHTS = str(ROOT / "data" / "post_insights.json")


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query, keep_blank_values=True)


class WeightedLengthTests(unittest.TestCase):
    def test_ascii_counts_one_per_character(self) -> None:
        self.assertEqual(composer.weighted_length("hello world"), 11)

    def test_chinese_counts_two_per_character(self) -> None:
        self.assertEqual(composer.weighted_length("你好"), 4)

    def test_japanese_counts_two_per_character(self) -> None:
        self.assertEqual(composer.weighted_length("こんにちは"), 10)

    def test_single_emoji_counts_two(self) -> None:
        self.assertEqual(composer.weighted_length("\U0001f973"), 2)

    def test_zwj_family_emoji_counts_two(self) -> None:
        self.assertEqual(composer.weighted_length("\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"), 2)

    def test_skin_tone_modifier_counts_two(self) -> None:
        self.assertEqual(composer.weighted_length("\U0001f44d\U0001f3fd"), 2)

    def test_flag_counts_two(self) -> None:
        self.assertEqual(composer.weighted_length("\U0001f1e8\U0001f1f3"), 2)

    def test_keycap_and_variation_selector_count_two(self) -> None:
        self.assertEqual(composer.weighted_length("1\ufe0f\u20e3"), 2)

    def test_single_url_counts_twenty_three(self) -> None:
        self.assertEqual(composer.weighted_length("https://example.com/very/long/path/here"), 23)

    def test_url_with_trailing_punctuation_counts_url_as_twenty_three(self) -> None:
        # twitter-text strips the trailing dots from the URL match. The dots are
        # ordinary text, so they still count one each (23 + 3 here).
        self.assertEqual(composer.weighted_length("https://example.com/a?b=1&c=2..."), 26)
        self.assertEqual(composer.weighted_length("see https://example.com/a."), 28)

    def test_two_urls_are_counted_separately(self) -> None:
        self.assertEqual(composer.weighted_length("https://a.example https://b.example"), 47)

    def test_exactly_280_is_valid(self) -> None:
        report = composer.validate_post("x" * 280)
        self.assertTrue(report["valid"])
        self.assertEqual(report["remaining"], 0)

    def test_281_is_too_long(self) -> None:
        report = composer.validate_post("x" * 281)
        self.assertFalse(report["valid"])
        self.assertIn("TOO_LONG", [item["code"] for item in report["issues"]])
        self.assertEqual(report["remaining"], -1)

    def test_empty_and_whitespace_only(self) -> None:
        empty = composer.validate_post("")
        blank = composer.validate_post("   \n\t")
        self.assertEqual(empty["issues"][0]["code"], "EMPTY")
        self.assertEqual(blank["issues"][0]["code"], "WHITESPACE_ONLY")
        self.assertFalse(empty["valid"])
        self.assertFalse(blank["valid"])

    def test_non_string_rejected(self) -> None:
        with self.assertRaises(ComposeError) as ctx:
            composer.weighted_length(None)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")


class IntentUrlTests(unittest.TestCase):
    def test_chinese_newline_ampersand_hash_and_emoji_round_trip(self) -> None:
        text = "你好\n世界 & #tag \U0001f973"
        url = composer.tweet_intent_url(text)
        decoded = _query(url)
        self.assertEqual(decoded["text"], [text])
        self.assertIn("%0A", url)
        self.assertNotIn(" ", url)

    def test_text_is_empty_when_blank(self) -> None:
        self.assertNotIn("text=", composer.tweet_intent_url("   "))

    def test_tweet_intent_parameters(self) -> None:
        url = composer.tweet_intent_url(
            "hello",
            in_reply_to="https://x.com/a/status/123",
            url="https://example.com",
            hashtags=["#one", "two"],
            via="@starter",
        )
        query = _query(url)
        self.assertEqual(query["in_reply_to"], ["123"])
        self.assertEqual(query["url"], ["https://example.com"])
        self.assertEqual(query["hashtags"], ["one,two"])
        self.assertEqual(query["via"], ["starter"])
        self.assertTrue(url.startswith("https://x.com/intent/tweet?"))

    def test_tweet_intent_rejects_too_long_text(self) -> None:
        with self.assertRaises(ComposeError) as ctx:
            composer.tweet_intent_url("x" * 281)
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_tweet_intent_rejects_hashtag_with_space(self) -> None:
        with self.assertRaises(ComposeError):
            composer.tweet_intent_url("hi", hashtags=["two words"])

    def test_reply_intent_format(self) -> None:
        url = composer.reply_intent_url("twitter.com/someone/status/42", "hi there")
        query = _query(url)
        self.assertEqual(query["in_reply_to"], ["42"])
        self.assertEqual(query["text"], ["hi there"])
        self.assertTrue(url.startswith("https://x.com/intent/tweet?"))

    def test_reply_intent_without_text(self) -> None:
        url = composer.reply_intent_url("42")
        self.assertNotIn("text=", url)

    def test_like_intent_format(self) -> None:
        self.assertEqual(
            composer.like_intent_url("42"),
            "https://x.com/intent/like?tweet_id=42",
        )

    def test_repost_intent_uses_retweet_path(self) -> None:
        self.assertEqual(
            composer.repost_intent_url("42"),
            "https://x.com/intent/retweet?tweet_id=42",
        )

    def test_follow_intent_format(self) -> None:
        self.assertEqual(
            composer.follow_intent_url("@NASA"),
            "https://x.com/intent/follow?screen_name=NASA",
        )


class ParseRefTests(unittest.TestCase):
    def test_accepts_numeric_id(self) -> None:
        self.assertEqual(composer.parse_post_ref(" 1234567890 "), "1234567890")

    def test_accepts_x_and_twitter_and_mobile_urls(self) -> None:
        self.assertEqual(composer.parse_post_ref("https://x.com/a/status/111"), "111")
        self.assertEqual(
            composer.parse_post_ref("https://twitter.com/a/status/222?s=20&t=x"), "222"
        )
        self.assertEqual(
            composer.parse_post_ref("https://mobile.twitter.com/a/status/333/photo/1"),
            "333",
        )
        self.assertEqual(composer.parse_post_ref("x.com/a/status/444"), "444")

    def test_rejects_javascript_and_other_hosts(self) -> None:
        for value in (
            "javascript:alert(1)",
            "https://evil.example/a/status/1",
            "https://x.com.evil.example/a/status/1",
        ):
            with self.assertRaises(ComposeError) as ctx:
                composer.parse_post_ref(value)
            self.assertEqual(ctx.exception.code, "INVALID_POST_REF")

    def test_rejects_non_numeric_and_overlong_ids(self) -> None:
        for value in ("abc", "123456789012345678901", "https://x.com/a/status/12a"):
            with self.assertRaises(ComposeError) as ctx:
                composer.parse_post_ref(value)
            self.assertEqual(ctx.exception.code, "INVALID_POST_REF")

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ComposeError):
            composer.parse_post_ref("   ")


class ParseScreenNameTests(unittest.TestCase):
    def test_accepts_at_name_plain_and_url(self) -> None:
        self.assertEqual(composer.parse_screen_name("@NASA"), "NASA")
        self.assertEqual(composer.parse_screen_name("nasa"), "nasa")
        self.assertEqual(composer.parse_screen_name("https://x.com/NASA/"), "NASA")
        self.assertEqual(
            composer.parse_screen_name("https://mobile.twitter.com/NASA?lang=en"), "NASA"
        )

    def test_length_boundaries(self) -> None:
        self.assertEqual(composer.parse_screen_name("a" * 15), "a" * 15)
        with self.assertRaises(ComposeError) as ctx:
            composer.parse_screen_name("a" * 16)
        self.assertEqual(ctx.exception.code, "INVALID_SCREEN_NAME")

    def test_rejects_bad_characters_and_schemes(self) -> None:
        for value in ("", "@", "bad name", "bad-name", "javascript:alert(1)", "https://evil.example/a"):
            with self.assertRaises(ComposeError) as ctx:
                composer.parse_screen_name(value)
            self.assertEqual(ctx.exception.code, "INVALID_SCREEN_NAME")


class InteractionLinkTests(unittest.TestCase):
    def test_each_action(self) -> None:
        post = composer.interaction_link("post", text="hello")
        reply = composer.interaction_link("reply", post="9", text="yes")
        like = composer.interaction_link("like", post="https://x.com/a/status/9")
        repost = composer.interaction_link("repost", post="9")
        follow = composer.interaction_link("follow", screen_name="@NASA")
        self.assertEqual(post["intent_url"], "https://x.com/intent/tweet?text=hello")
        self.assertIn("in_reply_to=9", reply["intent_url"])
        self.assertIn("/intent/like?tweet_id=9", like["intent_url"])
        self.assertIn("/intent/retweet?tweet_id=9", repost["intent_url"])
        self.assertIn("/intent/follow?screen_name=NASA", follow["intent_url"])
        for item in (post, reply, like, repost, follow):
            self.assertTrue(item["requires_x_login"])
            self.assertFalse(item["posts_automatically"])
            self.assertEqual(item["action"], item["action"].lower())

    def test_invalid_action(self) -> None:
        with self.assertRaises(ComposeError) as ctx:
            composer.interaction_link("delete", post="9")
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_missing_arguments(self) -> None:
        for kwargs in ({"action": "reply"}, {"action": "like"}, {"action": "follow"}):
            with self.assertRaises(ComposeError):
                composer.interaction_link(**kwargs)  # type: ignore[arg-type]

    def test_target_payload(self) -> None:
        link = composer.interaction_link("follow", screen_name="https://x.com/NASA")
        self.assertEqual(link["target"], {"screen_name": "NASA"})


class SuggestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.insights = composer.load_insights(INSIGHTS)

    def _codes(self, text: str) -> list[str]:
        return [item["code"] for item in composer.suggest(text, self.insights)]

    def test_too_long_is_warn(self) -> None:
        tips = composer.suggest("x" * 400, self.insights)
        self.assertEqual(tips[0]["code"], "TOO_LONG")
        self.assertEqual(tips[0]["level"], "warn")

    def test_hashtags_trigger_tip(self) -> None:
        self.assertIn("HASHTAG_HEAVY", self._codes("#one #two and a sentence"))

    def test_single_hashtag_does_not_trigger(self) -> None:
        self.assertNotIn("HASHTAG_HEAVY", self._codes("#one and a sentence"))

    def test_link_triggers_tip(self) -> None:
        codes = self._codes("read this https://example.com/post")
        self.assertIn("LINK_IN_TEXT", codes)

    def test_mention_alone_does_not_trigger(self) -> None:
        codes = self._codes("thanks @NASA for the note, it helped a lot today")
        self.assertNotIn("ADD_MEDIA", codes)

    def test_long_plain_text_triggers_media_tip(self) -> None:
        text = "I rebuilt my morning routine around one tiny habit and tracked it for thirty days, writing down the number every single time so the result would be honest."
        self.assertIn("ADD_MEDIA", self._codes(text))

    def test_short_text_triggers_tip(self) -> None:
        self.assertIn("SHORT_POST", self._codes("tiny note"))

    def test_question_opener_is_not_penalised(self) -> None:
        codes = self._codes("What changed your week? I keep coming back to this one detail about focus.")
        self.assertNotIn("SHORT_POST", codes)
        self.assertNotIn("ADD_MEDIA", codes)

    def test_evidence_carries_sample_and_calibration(self) -> None:
        tips = composer.suggest("a #one #two post https://example.com", self.insights)
        evidence = [item["evidence"] for item in tips if item["evidence"]]
        self.assertTrue(evidence)
        for item in evidence:
            self.assertIn("Bluesky sample, n=43718, 2026-10-20", item)
            self.assertIn("not causation", item)

    def test_numbers_come_from_data_file(self) -> None:
        mutated = json.loads(json.dumps(self.insights))
        for row in mutated["top_decile_vs_rest"]["boolean_features"]:
            if row["feature"] == "has_hashtag":
                row["difference_pp"] = -9.87
        tips = composer.suggest("#one #two", mutated)
        text = " ".join(item["evidence"] or "" for item in tips)
        self.assertIn("-9.87pp", text)

    def test_empty_text_has_no_length_tips(self) -> None:
        self.assertEqual(composer.suggest("", self.insights), [])


class DraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.insights = composer.load_insights(INSIGHTS)

    def test_count_and_order(self) -> None:
        drafts = composer.draft_variants("a small CLI", count=2, insights=self.insights)
        self.assertEqual(len(drafts), 2)
        self.assertEqual([item["hook_type"] for item in drafts], ["story_first_person", "question"])
        self.assertEqual([item["id"] for item in drafts], ["d1", "d2"])

    def test_default_count_is_three(self) -> None:
        drafts = composer.draft_variants("a small CLI", insights=self.insights)
        self.assertEqual(len(drafts), 3)

    def test_all_valid_and_unique(self) -> None:
        drafts = composer.draft_variants("a much longer topic " * 4, cta="Tell me what you think", insights=self.insights)
        self.assertTrue(all(item["valid"] for item in drafts))
        self.assertEqual(len({item["text"] for item in drafts}), len(drafts))
        for item in drafts:
            self.assertLessEqual(item["weighted_length"], 280)
            self.assertIn("intent_url", item)
            self.assertIsInstance(item["suggestions"], list)

    def test_deterministic(self) -> None:
        first = composer.draft_variants("tiny bots", tone="warm", insights=self.insights)
        second = composer.draft_variants("tiny bots", tone="warm", insights=self.insights)
        self.assertEqual(first, second)

    def test_tones_change_text(self) -> None:
        clear = composer.draft_variants("tiny bots", tone="clear", count=1, insights=self.insights)
        technical = composer.draft_variants("tiny bots", tone="technical", count=1, insights=self.insights)
        self.assertNotEqual(clear[0]["text"], technical[0]["text"])

    def test_invalid_tone_and_topic(self) -> None:
        with self.assertRaises(ComposeError):
            composer.draft_variants("x", tone="loud", insights=self.insights)
        with self.assertRaises(ComposeError):
            composer.draft_variants("   ", insights=self.insights)

    def test_chinese_topic_still_fits(self) -> None:
        drafts = composer.draft_variants("中文主题" * 20, insights=self.insights)
        self.assertTrue(all(item["valid"] for item in drafts))


class InsightsTests(unittest.TestCase):
    def test_build_output_has_no_post_text(self) -> None:
        payload = build_insights.build()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "post_insights.json"
            build_insights.write(payload, target)
            raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
        # "text" appears as a media alias value; assert no post body field or post URL leaks.
        self.assertNotIn('"text":', raw)
        self.assertNotIn("bsky.app/profile", raw)
        self.assertNotIn("at://", raw)
        self.assertEqual(data["schema"], "x-starter-post-insights/v1")
        self.assertTrue(data["source"]["not_x_data"])
        self.assertIn("lift_median", data["by_media"][0])
        self.assertIn("lift_median_ci95", data["by_length_bucket"][0])
        self.assertIn("difference_pp", data["top_decile_vs_rest"]["media"][0])

    def test_checked_in_data_matches_builder(self) -> None:
        path = Path(INSIGHTS)
        self.assertTrue(path.exists(), "run build_insights.py first")
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, build_insights.build())

    def test_missing_file_raises_compose_error(self) -> None:
        with self.assertRaises(ComposeError) as ctx:
            composer.load_insights(str(Path(tempfile.gettempdir()) / "no-such-insights.json"))
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_concurrent_loads_agree(self) -> None:
        results: list[dict] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(16)

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                results.append(composer.load_insights(INSIGHTS))
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 16)
        first = results[0]
        self.assertTrue(all(item == first for item in results))
        # Callers get copies, so a local edit cannot poison the cache.
        results[0]["source"]["post_count"] = 1
        self.assertNotEqual(composer.load_insights(INSIGHTS)["source"]["post_count"], 1)


if __name__ == "__main__":
    unittest.main()
