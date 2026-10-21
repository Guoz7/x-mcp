"""Post composer engine built on X Web Intents.

Every link produced here opens X's own composer or action page. The user
finishes the action inside X, so no developer app, OAuth token, or API fee is
involved. The module only prepares text, validates length with twitter-text
v3 weights, and builds intent URLs.

Standard library only.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

__all__ = [
    "MAX_WEIGHTED_LENGTH",
    "INTENT_BASE",
    "ComposeError",
    "weighted_length",
    "validate_post",
    "parse_post_ref",
    "parse_screen_name",
    "tweet_intent_url",
    "like_intent_url",
    "repost_intent_url",
    "reply_intent_url",
    "follow_intent_url",
    "interaction_link",
    "load_insights",
    "suggest",
    "draft_variants",
]

MAX_WEIGHTED_LENGTH = 280
INTENT_BASE = "https://x.com/intent"

# twitter-text v3 configuration (see .agent-work/pricing/twitter-text-v3.json).
_WEIGHT_SCALE = 100
_DEFAULT_WEIGHT = 200
# Spans are counted in raw scale units; one plain character outside the
# CJK/emoji ranges costs 100, which is one weighted character.
_URL_WEIGHT = 23 * _WEIGHT_SCALE
_EMOJI_WEIGHT = 2 * _WEIGHT_SCALE
_WEIGHT_RANGES: tuple[tuple[int, int], ...] = (
    (0, 4351),
    (8192, 8205),
    (8208, 8223),
    (8242, 8247),
)

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_URL_TRAILING = ".,!?;:)]}\"'"
_EMOJI_BASE = (
    "[\u203c\u2049\u2194-\u21aa\u231a-\u231b"
    "\u2328\u23cf\u23e9-\u23f3\u23f8-\u23fa\u24c2\u25aa-\u25ab\u25b6\u25c0"
    "\u25fb-\u25fe\u2600-\u27bf\u2934-\u2935\u2b05-\u2b07\u2b1b-\u2b1c"
    "\u2b50\u2b55\u3030\u303d\u3297\u3299\U0001f000-\U0001faff]"
)
_EMOJI_MOD = "(?:\ufe0f|[\U0001f3fb-\U0001f3ff])"
_EMOJI_RE = re.compile(
    r"(?:[\U0001f1e6-\U0001f1ff]{2})"
    r"|(?:[#*0-9]\ufe0f?\u20e3)"
    # These four sit inside a narrow-weight range, so they only cost 2 when
    # the variation selector asks for emoji presentation.
    r"|(?:[\u00a9\u00ae\u2122\u2139]\ufe0f)"
    r"|(?:"
    + _EMOJI_BASE
    + _EMOJI_MOD
    + r"*(?:\u200d"
    + _EMOJI_BASE
    + _EMOJI_MOD
    + r"*)*)"
)

_X_HOSTS = {
    "x.com",
    "www.x.com",
    "mobile.x.com",
    "m.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
    "m.twitter.com",
}
_SCREEN_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_POST_ID_RE = re.compile(r"^[0-9]{1,20}$")
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@[A-Za-z0-9_]{1,15}")
_HASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_])#[^\W_][\w]*", re.UNICODE)


class ComposeError(ValueError):
    """A composer error that is safe to show to an MCP caller."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _codepoint_weight(char: str) -> int:
    point = ord(char)
    for start, end in _WEIGHT_RANGES:
        if start <= point <= end:
            return 100
    return _DEFAULT_WEIGHT


def _scan_tokens(text: str) -> tuple[int, int]:
    """Return (weight, url_count) for one piece of text."""
    spans: list[tuple[int, int, int]] = []
    url_count = 0
    for match in _URL_RE.finditer(text):
        raw = match.group(0)
        trimmed = raw.rstrip(_URL_TRAILING)
        if len(trimmed) <= len("https://"):
            continue
        spans.append((match.start(), match.start() + len(trimmed), _URL_WEIGHT))
        url_count += 1
    for match in _EMOJI_RE.finditer(text):
        spans.append((match.start(), match.end(), _EMOJI_WEIGHT))

    # Longer spans win at the same position so a URL is never split by an
    # emoji match, and every later overlap is skipped.
    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    total = 0
    cursor = 0
    for start, end, weight in spans:
        if start < cursor:
            continue
        for char in text[cursor:start]:
            total += _codepoint_weight(char)
        total += weight
        cursor = end
    for char in text[cursor:]:
        total += _codepoint_weight(char)
    return total, url_count


def weighted_length(text: str) -> int:
    """Return the twitter-text v3 weighted length of ``text``."""
    if not isinstance(text, str):
        raise ComposeError("INVALID_ARGUMENT", "text must be a string")
    total, _ = _scan_tokens(text)
    return total // _WEIGHT_SCALE


def _clean_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ComposeError("INVALID_ARGUMENT", f"{field} must be a string")
    # Normalise lone surrogates so URL encoding never fails.
    return value.encode("utf-8", "surrogatepass").decode("utf-8", "replace")


def validate_post(text: str) -> dict[str, Any]:
    """Check length and emptiness for one post."""
    cleaned = _clean_text(text, "text")
    length_weight, url_count = _scan_tokens(cleaned)
    length = length_weight // _WEIGHT_SCALE
    issues: list[dict[str, str]] = []
    if cleaned == "":
        issues.append({"code": "EMPTY", "message": "The post is empty."})
    elif cleaned.strip() == "":
        issues.append(
            {"code": "WHITESPACE_ONLY", "message": "The post has no visible characters."}
        )
    if length > MAX_WEIGHTED_LENGTH:
        issues.append(
            {
                "code": "TOO_LONG",
                "message": (
                    f"The post is {length} weighted characters; the limit is "
                    f"{MAX_WEIGHTED_LENGTH}."
                ),
            }
        )
    return {
        "weighted_length": length,
        "max": MAX_WEIGHTED_LENGTH,
        "remaining": MAX_WEIGHTED_LENGTH - length,
        "valid": not issues,
        "url_count": url_count,
        "issues": issues,
    }


def _status_id_from_path(path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    for index, part in enumerate(parts):
        if part == "status" and index + 1 < len(parts):
            candidate = parts[index + 1]
            if _POST_ID_RE.match(candidate):
                return candidate
    return None


def parse_post_ref(value: str) -> str:
    """Accept a numeric post id or an X/Twitter status URL and return the id."""
    if not isinstance(value, str):
        raise ComposeError("INVALID_POST_REF", "The post reference must be a string.")
    raw = value.strip()
    if not raw:
        raise ComposeError("INVALID_POST_REF", "The post reference is empty.")
    lowered = raw.lower()
    if lowered.startswith(("javascript:", "data:", "file:", "vbscript:")):
        raise ComposeError("INVALID_POST_REF", "Only X post ids or X status URLs are accepted.")
    if _POST_ID_RE.match(raw):
        return raw

    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise ComposeError("INVALID_POST_REF", "Only X post ids or X status URLs are accepted.")
    host = (parsed.hostname or "").lower()
    if host not in _X_HOSTS:
        raise ComposeError(
            "INVALID_POST_REF", "The URL must point at x.com or twitter.com."
        )
    post_id = _status_id_from_path(parsed.path)
    if post_id is None:
        raise ComposeError(
            "INVALID_POST_REF", "The URL has no numeric /status/<id> segment."
        )
    return post_id


def parse_screen_name(value: str) -> str:
    """Accept '@name', 'name', or a profile URL and return 'name'."""
    if not isinstance(value, str):
        raise ComposeError("INVALID_SCREEN_NAME", "The screen name must be a string.")
    raw = value.strip()
    if not raw:
        raise ComposeError("INVALID_SCREEN_NAME", "The screen name is empty.")
    lowered = raw.lower()
    if lowered.startswith(("javascript:", "data:", "file:", "vbscript:")):
        raise ComposeError("INVALID_SCREEN_NAME", "Only X screen names or X profile URLs are accepted.")
    candidate = raw
    if "://" in raw or raw.lower().startswith(("x.com/", "twitter.com/", "www.x.com/")):
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = (parsed.hostname or "").lower()
        if host not in _X_HOSTS:
            raise ComposeError(
                "INVALID_SCREEN_NAME", "The URL must point at x.com or twitter.com."
            )
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ComposeError("INVALID_SCREEN_NAME", "The profile URL has no screen name.")
        candidate = parts[0][1:] if parts[0].startswith("@") else parts[0]
    else:
        candidate = raw[1:] if raw.startswith("@") else raw
    if not _SCREEN_NAME_RE.match(candidate):
        raise ComposeError(
            "INVALID_SCREEN_NAME",
            "A screen name is 1-15 characters from A-Z, a-z, 0-9, or _.",
        )
    return candidate


def _encode(params: list[tuple[str, str]]) -> str:
    return urlencode([(key, val) for key, val in params if val != ""], quote_via=quote)


def tweet_intent_url(
    text: str,
    *,
    in_reply_to: str | None = None,
    url: str | None = None,
    hashtags: list[str] | None = None,
    via: str | None = None,
) -> str:
    """Build a /intent/tweet link with optional reply, URL, and hashtags."""
    body = _clean_text(text, "text")
    params: list[tuple[str, str]] = []
    if body.strip():
        if weighted_length(body) > MAX_WEIGHTED_LENGTH:
            raise ComposeError(
                "INVALID_ARGUMENT",
                f"text exceeds {MAX_WEIGHTED_LENGTH} weighted characters",
            )
        params.append(("text", body))
    if in_reply_to is not None:
        params.append(("in_reply_to", parse_post_ref(in_reply_to)))
    if url is not None:
        params.append(("url", _clean_text(url, "url")))
    if hashtags:
        cleaned: list[str] = []
        for tag in hashtags:
            if not isinstance(tag, str):
                raise ComposeError("INVALID_ARGUMENT", "hashtags must be strings")
            value = tag.strip().lstrip("#")
            if not value:
                continue
            if any(char in value for char in ", \t\n\r"):
                raise ComposeError(
                    "INVALID_ARGUMENT", "A hashtag cannot contain spaces or commas."
                )
            cleaned.append(value)
        if cleaned:
            params.append(("hashtags", ",".join(cleaned)))
    if via is not None:
        params.append(("via", parse_screen_name(via)))
    query = _encode(params)
    return f"{INTENT_BASE}/tweet?{query}" if query else f"{INTENT_BASE}/tweet"


def like_intent_url(post: str) -> str:
    return f"{INTENT_BASE}/like?{_encode([('tweet_id', parse_post_ref(post))])}"


def repost_intent_url(post: str) -> str:
    return f"{INTENT_BASE}/retweet?{_encode([('tweet_id', parse_post_ref(post))])}"


def reply_intent_url(post: str, text: str = "") -> str:
    body = _clean_text(text, "text")
    params: list[tuple[str, str]] = [("in_reply_to", parse_post_ref(post))]
    if body.strip():
        if weighted_length(body) > MAX_WEIGHTED_LENGTH:
            raise ComposeError(
                "INVALID_ARGUMENT",
                f"text exceeds {MAX_WEIGHTED_LENGTH} weighted characters",
            )
        params.append(("text", body))
    return f"{INTENT_BASE}/tweet?{_encode(params)}"


def follow_intent_url(screen_name: str) -> str:
    return f"{INTENT_BASE}/follow?{_encode([('screen_name', parse_screen_name(screen_name))])}"


_ACTIONS = ("post", "reply", "like", "repost", "follow")


def interaction_link(
    action: str,
    *,
    post: str | None = None,
    screen_name: str | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """Return one intent link plus the contract a caller can display."""
    if not isinstance(action, str) or action.lower() not in _ACTIONS:
        raise ComposeError(
            "INVALID_ARGUMENT",
            "action must be one of post, reply, like, repost, follow",
        )
    kind = action.lower()
    target: dict[str, Any] = {}
    if kind == "post":
        intent = tweet_intent_url(text or "")
        if text:
            target["text"] = text
    elif kind == "reply":
        if post is None:
            raise ComposeError("INVALID_ARGUMENT", "reply needs a post reference")
        post_id = parse_post_ref(post)
        intent = reply_intent_url(post_id, text or "")
        target["post_id"] = post_id
        if text:
            target["text"] = text
    elif kind == "like":
        if post is None:
            raise ComposeError("INVALID_ARGUMENT", "like needs a post reference")
        post_id = parse_post_ref(post)
        intent = like_intent_url(post_id)
        target["post_id"] = post_id
    elif kind == "repost":
        if post is None:
            raise ComposeError("INVALID_ARGUMENT", "repost needs a post reference")
        post_id = parse_post_ref(post)
        intent = repost_intent_url(post_id)
        target["post_id"] = post_id
    else:
        if screen_name is None:
            raise ComposeError("INVALID_ARGUMENT", "follow needs a screen name")
        name = parse_screen_name(screen_name)
        intent = follow_intent_url(name)
        target["screen_name"] = name
    return {
        "action": kind,
        "intent_url": intent,
        "target": target,
        "requires_x_login": True,
        "posts_automatically": False,
    }


_DEFAULT_INSIGHTS_PATH = Path(__file__).resolve().parent / "data" / "post_insights.json"
_INSIGHTS_LOCK = threading.RLock()
_INSIGHTS_CACHE: dict[str, dict[str, Any]] = {}


def load_insights(path: str | None = None) -> dict[str, Any]:
    """Load and cache the compact insights file (thread-safe)."""
    target = Path(path) if path else _DEFAULT_INSIGHTS_PATH
    try:
        key = str(target.resolve())
    except OSError:
        key = str(target)
    with _INSIGHTS_LOCK:
        cached = _INSIGHTS_CACHE.get(key)
        if cached is not None:
            return copy.deepcopy(cached)
        try:
            with target.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            raise ComposeError(
                "INVALID_ARGUMENT",
                f"Insights data was not found at {target}. Run build_insights.py first.",
            ) from None
        except (OSError, json.JSONDecodeError) as exc:
            raise ComposeError(
                "INVALID_ARGUMENT",
                f"Insights data cannot be read ({type(exc).__name__}).",
            ) from None
        if not isinstance(data, dict):
            raise ComposeError("INVALID_ARGUMENT", "Insights data must be a JSON object.")
        _INSIGHTS_CACHE[key] = data
        return copy.deepcopy(data)


def _calibration(insights: dict[str, Any]) -> str:
    source = insights.get("source") or {}
    count = source.get("post_count", "unknown")
    date = source.get("date", "unknown")
    return f"Bluesky sample, n={count}, {date}; correlation, not causation"


def _first(insights: dict[str, Any], collection: str, key: str) -> dict[str, Any] | None:
    for entry in insights.get(collection) or []:
        if isinstance(entry, dict):
            if key in (entry.get("key"), entry.get("feature"), entry.get("alias")):
                return entry
    return None


def _top_feature(insights: dict[str, Any], collection: str, key: str) -> dict[str, Any] | None:
    top = insights.get("top_decile_vs_rest")
    if not isinstance(top, dict):
        return None
    for entry in top.get(collection) or []:
        if isinstance(entry, dict) and key in (
            entry.get("feature"),
            entry.get("key"),
            entry.get("alias"),
        ):
            return entry
    return None


_QUESTION_STARTS = (
    "what",
    "why",
    "how",
    "when",
    "where",
    "who",
    "which",
    "is",
    "are",
    "do",
    "does",
    "can",
    "should",
    "would",
    "could",
)
_FIRST_PERSON_STARTS = ("i ", "i'", "we ", "we'", "my ", "our ")


def _opening_hook(text: str) -> str | None:
    """Classify the first line so soft length advice does not fight the hook."""
    stripped = text.strip()
    if not stripped:
        return None
    first = stripped.splitlines()[0].strip()
    if first.endswith("?") or first.lower().startswith(_QUESTION_STARTS):
        return "question"
    if first.lower().startswith(_FIRST_PERSON_STARTS) and len(first) >= 40:
        return "story_first_person"
    return None


def _suggestion(code: str, level: str, message: str, evidence: str | None) -> dict[str, Any]:
    return {"code": code, "level": level, "message": message, "evidence": evidence}


def suggest(text: str, insights: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return honest, data-backed tips for one draft."""
    body = _clean_text(text, "text")
    if body.strip() == "":
        return []
    report = validate_post(body)
    data = insights if insights is not None else load_insights()
    calibration = _calibration(data)
    out: list[dict[str, Any]] = []
    opening = _opening_hook(body)

    length = report["weighted_length"]
    if length > MAX_WEIGHTED_LENGTH:
        out.append(
            _suggestion(
                "TOO_LONG",
                "warn",
                f"Shorten the post to {MAX_WEIGHTED_LENGTH} weighted characters or less.",
                None,
            )
        )

    hashtags = _HASHTAG_RE.findall(body)
    if len(hashtags) >= 2:
        feature = _top_feature(data, "boolean_features", "has_hashtag")
        if feature:
            evidence = (
                "Hashtags appear in {top:.2f}% of head-10% posts versus {rest:.2f}% "
                "of the rest ({diff:+.2f}pp). {cal}"
            ).format(
                top=float(feature.get("top_share_pct", 0.0)),
                rest=float(feature.get("rest_share_pct", 0.0)),
                diff=float(feature.get("difference_pp", 0.0)),
                cal=calibration,
            )
            out.append(
                _suggestion(
                    "HASHTAG_HEAVY",
                    "tip",
                    "Two or more hashtags is a weak pattern in this sample; keep one at most.",
                    evidence,
                )
            )
        else:
            out.append(
                _suggestion(
                    "HASHTAG_HEAVY",
                    "tip",
                    "Two or more hashtags is a weak pattern; keep one at most.",
                    None,
                )
            )

    if report["url_count"] > 0:
        feature = _top_feature(data, "media", "link")
        link_note = (
            "X's API charges per post that contains a link; a Web Intent post has no API fee."
        )
        if feature:
            evidence = (
                "Posts with a link are {top:.2f}% of head-10% posts versus {rest:.2f}% "
                "of the rest ({diff:+.2f}pp). {cal}"
            ).format(
                top=float(feature.get("top_share_pct", 0.0)),
                rest=float(feature.get("rest_share_pct", 0.0)),
                diff=float(feature.get("difference_pp", 0.0)),
                cal=calibration,
            )
            out.append(
                _suggestion(
                    "LINK_IN_TEXT",
                    "tip",
                    f"Consider moving the link to a reply. {link_note}",
                    evidence,
                )
            )
        else:
            out.append(
                _suggestion(
                    "LINK_IN_TEXT",
                    "tip",
                    f"Consider moving the link to a reply. {link_note}",
                    None,
                )
            )

    # Study rule: "plain text" means no link, image, video, or quote. A mention
    # does not change that bucket.
    plain_text = report["url_count"] == 0
    if plain_text and length >= 140:
        image = _top_feature(data, "media", "image")
        video = _first(data, "by_media", "video")
        pieces: list[str] = []
        if image:
            pieces.append(
                "Posts with an image are {top:.2f}% of head-10% posts versus {rest:.2f}% "
                "of the rest ({diff:+.2f}pp).".format(
                    top=float(image.get("top_share_pct", 0.0)),
                    rest=float(image.get("rest_share_pct", 0.0)),
                    diff=float(image.get("difference_pp", 0.0)),
                )
            )
        if video:
            pieces.append(
                "Video posts have a median lift of {lift:.4f}.".format(
                    lift=float(video.get("lift_median", 0.0))
                )
            )
        evidence = (" ".join(pieces) + " " + calibration) if pieces else None
        out.append(
            _suggestion(
                "ADD_MEDIA",
                "tip",
                "A long text-only post may do better with an image or a short video.",
                evidence,
            )
        )

    if length < 80 and opening is None:
        feature = _top_feature(data, "length_bucket", "b241_300")
        if feature:
            evidence = (
                "Posts of 241-300 characters are {top:.2f}% of head-10% posts versus "
                "{rest:.2f}% of the rest ({diff:+.2f}pp). {cal}"
            ).format(
                top=float(feature.get("top_share_pct", 0.0)),
                rest=float(feature.get("rest_share_pct", 0.0)),
                diff=float(feature.get("difference_pp", 0.0)),
                cal=calibration,
            )
            out.append(
                _suggestion(
                    "SHORT_POST",
                    "tip",
                    "Short posts are common here; this sample's head-10% skews longer.",
                    evidence,
                )
            )
        else:
            out.append(
                _suggestion(
                    "SHORT_POST",
                    "tip",
                    "Short posts are common here; consider adding one concrete detail.",
                    None,
                )
            )

    return out


_HOOK_ORDER: tuple[str, ...] = (
    "story_first_person",
    "question",
    "number_list",
    "contrarian",
    "announcement",
)

_TONE_INDEX = {"clear": 0, "warm": 1, "technical": 2}

_TEMPLATES: dict[str, tuple[str, ...]] = {
    "story_first_person": (
        "I spent the last month on {topic}. The change was smaller than I expected: "
        "one habit, one number, one honest lesson.",
        "I spent the last month on {topic}, and it went better than I feared. "
        "Small steps, steady check-ins, no drama.",
        "I spent the last month on {topic}. One input, one output, one variable "
        "changed at a time.",
    ),
    "question": (
        "What would you change about {topic}? I keep coming back to one detail and "
        "I want a second opinion.",
        "Honest question about {topic}: what worked for you, and what did you drop?",
        "Which part of {topic} actually moves the number? I have a guess and I would "
        "like to test it.",
    ),
    "number_list": (
        "3 things I learned about {topic}:\n1) start smaller than feels right\n"
        "2) measure one number\n3) ship before it feels finished.",
        "3 small wins with {topic}:\n1) a shorter first step\n2) one weekly check\n"
        "3) telling someone out loud.",
        "3 notes on {topic}:\n1) define the metric\n2) test one variable\n"
        "3) write down the result.",
    ),
    "contrarian": (
        "Unpopular opinion: most advice about {topic} is backwards. The boring "
        "version works better than the clever one.",
        "I used to overthink {topic}. The version that finally worked was the "
        "least impressive one.",
        "Most {topic} advice optimises the wrong step. The constraint is usually "
        "upstream, not at the tactic.",
    ),
    "announcement": (
        "Just shipped: {topic}. Small scope, clear goal, short feedback loop.",
        "New this week: {topic}. It is live, it is small, and I would love a look.",
        "Shipped: {topic}. Scope held to one goal; the next step is a measured "
        "follow-up.",
    ),
}


def _fit_text(build: Any, topic: str, cta: str) -> str:
    candidate = topic.strip() or "a small idea"
    for _ in range(64):
        body = build(candidate)
        if cta.strip():
            body = f"{body}\n\n{cta.strip()}"
        if weighted_length(body) <= MAX_WEIGHTED_LENGTH:
            return body
        if len(candidate) <= 3:
            break
        candidate = candidate[: max(3, int(len(candidate) * 0.75))].rstrip() + "..."
    body = build(candidate)
    if cta.strip():
        body = f"{body}\n\n{cta.strip()}"
    return _hard_truncate(body, MAX_WEIGHTED_LENGTH)


def _hard_truncate(text: str, limit: int) -> str:
    if weighted_length(text) <= limit:
        return text
    kept: list[str] = []
    for index in range(1, len(text) + 1):
        if weighted_length(text[:index]) > limit - 3:
            break
        kept.append(text[index - 1])
    return "".join(kept).rstrip() + "..."


def draft_variants(
    topic: str,
    *,
    tone: str = "clear",
    cta: str = "",
    count: int = 3,
    insights: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build deterministic draft variants; no model is called."""
    subject = _clean_text(topic, "topic").strip()
    if not subject:
        raise ComposeError("INVALID_ARGUMENT", "topic must not be empty")
    call_to_action = _clean_text(cta, "cta")
    if tone not in _TONE_INDEX:
        raise ComposeError("INVALID_ARGUMENT", "tone must be clear, warm, or technical")
    if not isinstance(count, int) or isinstance(count, bool):
        raise ComposeError("INVALID_ARGUMENT", "count must be an integer")
    wanted = max(1, min(count, len(_HOOK_ORDER)))
    data = insights if insights is not None else load_insights()
    index = _TONE_INDEX[tone]

    drafts: list[dict[str, Any]] = []
    for position, hook in enumerate(_HOOK_ORDER[:wanted], start=1):
        template = _TEMPLATES[hook][index]

        def build(value: str, template: str = template) -> str:
            return template.format(topic=value)

        body = _fit_text(build, subject, call_to_action)
        report = validate_post(body)
        drafts.append(
            {
                "id": f"d{position}",
                "hook_type": hook,
                "text": body,
                "weighted_length": report["weighted_length"],
                "valid": report["valid"],
                "intent_url": tweet_intent_url(body),
                "suggestions": suggest(body, data),
            }
        )
    return drafts


def _debug_encoding_roundtrip(url: str) -> dict[str, list[str]]:
    """Small helper used by tests to prove query encoding is reversible."""
    return parse_qs(urlparse(url).query, keep_blank_values=True)


def _grapheme_name(char: str) -> str:
    try:
        return unicodedata.name(char)
    except ValueError:
        return "UNKNOWN"
