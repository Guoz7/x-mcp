"""Credential-free reads of public X content.

The X API needs a developer app, a token, and a paid package. Public profile
pages, the public embed timeline, and the public tweet-result endpoint do not.
This module reads only those three surfaces, sends no cookie or token, and
returns one stable shape for every caller.

It never writes. Every result carries the source URL, a fetch time, and a cache
flag so a caller can tell a live read from a cached one.

Only the Python standard library is used.
"""

from __future__ import annotations

import email.utils
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "PublicReadError",
    "PublicXClient",
    "extract_profile",
    "extract_timeline",
    "normalize_post",
]

PROFILE_URL = "https://x.com/{screen_name}"
TIMELINE_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{screen_name}"
POST_URL = "https://cdn.syndication.twimg.com/tweet-result?id={post_id}&lang=en&token=a"
USER_AGENT = "x-starter/0.8 (+public-read; no credentials)"

SCREEN_NAME_RE = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
POST_ID_RE = re.compile(r"[0-9]{1,25}\Z")

_UNESCAPE_MAP = {
    '\\"': '"',
    "\\\\": "\\",
    "\\n": "\n",
    "\\t": "\t",
    "\\r": "\r",
    "\\/": "/",
    "\\u003c": "<",
    "\\u003e": ">",
    "\\u0026": "&",
}


class PublicReadError(Exception):
    """A failure that is safe to return through an MCP tool result."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status


def _plain(value: str) -> str:
    """Collapse X's nested JSON-in-JSON escaping into plain JSON text.

    X embeds Relay payloads as a string inside HTML. One field can be escaped
    two or three times, so a single replace pass is not enough.
    """

    text = value or ""
    for _ in range(4):
        updated = text.replace('\\\\"', '"').replace('\\"', '"')
        if updated == text:
            break
        text = updated
    return text


def _validate_screen_name(value: str) -> str:
    name = str(value or "").strip()
    if "/" in name:
        match = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})", name)
        if not match:
            raise PublicReadError(
                "INVALID_SCREEN_NAME",
                "A profile URL must look like https://x.com/<name>.",
            )
        name = match.group(1)
    name = name.lstrip("@")
    if not SCREEN_NAME_RE.fullmatch(name):
        raise PublicReadError(
            "INVALID_SCREEN_NAME",
            "screen_name must be 1-15 letters, digits, or underscores.",
        )
    return name


def _validate_post_id(value: str) -> str:
    raw = str(value or "").strip()
    match = re.search(r"/status/(\d{1,25})", raw)
    if match:
        raw = match.group(1)
    if not POST_ID_RE.fullmatch(raw):
        raise PublicReadError("INVALID_POST_ID", "post_id must be a numeric status id.")
    return raw


def _unescape(value: str) -> str:
    """Turn JSON string escapes into readable text."""

    for source, target in _UNESCAPE_MAP.items():
        value = value.replace(source, target)
    return value


def _iso(value: str | None) -> str | None:
    if not value:
        return None
    if re.match(r"\d{4}-\d{2}-\d{2}T", value):
        return value.replace(".000Z", "Z").replace("+00:00", "Z")
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.isoformat() + "Z"
    return parsed.isoformat()


def _first_int(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def extract_profile(html: str, screen_name: str) -> dict[str, Any]:
    """Parse the public profile page.

    X renders profile data as escaped Relay fragments. ``_plain`` flattens the
    escaping, so the patterns below read plain JSON-like text.
    """

    name = _validate_screen_name(screen_name)
    text = _plain(html or "")
    followers = _first_int(r'"relationship_counts":\{"followers":(\d+)', text)
    following = _first_int(
        r'"relationship_counts":\{"followers":\d+,"following":(\d+)', text
    )
    posts = _first_int(r'"tweet_counts":\{"tweets":(\d+)', text)

    display = re.search(r'"core":\{"name":"(.*?)","screen_name":"(.*?)"\}', text)
    bio = re.search(r'"profile_bio":\{"description":"(.*?)","entities":', text, re.S)
    # The user object carries subscribers_count right after its id; the tweet
    # object that also appears on the page does not.
    user_id = re.search(r'"rest_id":"(\d+)","subscribers_count"', text)
    avatar = re.search(r'"avatar":\{"image_url":"(https://pbs\.twimg\.com/[^"]+)', text)
    blue = re.search(r'"is_blue_verified":(true|false)', text)
    protected = re.search(r'"protected":(true|false)', text)

    if followers is None and posts is None and display is None:
        raise PublicReadError(
            "PROFILE_NOT_PARSED",
            f"X returned a profile page for @{name} with no recognizable profile fields.",
        )

    return {
        "screen_name": display.group(2) if display else name,
        "name": _unescape(display.group(1)) if display else name,
        "user_id": user_id.group(1) if user_id else None,
        "bio": _unescape(bio.group(1)).strip() if bio else "",
        "metrics": {
            "followers": followers,
            "following": following,
            "posts": posts,
        },
        "avatar_url": _unescape(avatar.group(1)) if avatar else None,
        "blue_verified": (blue.group(1) == "true") if blue else None,
        "protected": (protected.group(1) == "true") if protected else None,
        "url": PROFILE_URL.format(screen_name=name),
    }


def normalize_post(tweet: dict[str, Any]) -> dict[str, Any]:
    """Map a syndication tweet object onto one stable shape."""

    author = (tweet.get("user") or {}).get("screen_name") or ""
    post_id = str(tweet.get("id_str") or tweet.get("id") or "")
    permalink = tweet.get("permalink") or f"/{author}/status/{post_id}"
    url = permalink if permalink.startswith("http") else f"https://x.com{permalink}"
    entities = tweet.get("entities") or {}
    media = entities.get("media") or tweet.get("mediaDetails") or []
    return {
        "id": post_id,
        "url": url,
        "author": author,
        "author_name": (tweet.get("user") or {}).get("name") or author,
        "text": tweet.get("text") or tweet.get("full_text") or "",
        "created_at": _iso(tweet.get("created_at")),
        "lang": tweet.get("lang"),
        "conversation_id": tweet.get("conversation_id_str"),
        "metrics": {
            "likes": tweet.get("favorite_count"),
            "replies": tweet.get("reply_count"),
            "reposts": tweet.get("retweet_count"),
            "quotes": tweet.get("quote_count"),
            "replies_total": tweet.get("conversation_count"),
        },
        "media_count": len(media) if isinstance(media, list) else 0,
        "possibly_sensitive": bool(tweet.get("possibly_sensitive")),
    }


def extract_timeline(payload: str, screen_name: str, limit: int) -> list[dict[str, Any]]:
    """Parse the embed timeline page, or the newest post on a profile page."""

    name = _validate_screen_name(screen_name)
    text = payload or ""
    next_data = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        text,
        re.S,
    )
    if next_data:
        try:
            document = json.loads(next_data.group(1))
        except json.JSONDecodeError as exc:
            raise PublicReadError(
                "TIMELINE_NOT_PARSED", "The embed timeline returned invalid JSON."
            ) from exc
        page = (
            document.get("props", {}).get("pageProps", {})
            if isinstance(document, dict)
            else {}
        )
        entries = (page.get("timeline") or {}).get("entries") or []
        posts: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content") or {}
            tweet = content.get("tweet") if isinstance(content, dict) else None
            if isinstance(tweet, dict) and (tweet.get("text") or tweet.get("full_text")):
                posts.append(normalize_post(tweet))
            if len(posts) >= limit:
                break
        if posts:
            return posts
        # A protected or empty account returns a well-formed page with no entries.
        if page.get("latest_tweet_id"):
            return []

    # Fallback: the profile page embeds the newest post as escaped relay data.
    plain = _plain(text)
    post = _embedded_post(plain, name)
    if post is not None:
        return [post]

    raise PublicReadError(
        "TIMELINE_NOT_PARSED",
        f"X returned no readable posts for @{name}. The account may be protected, "
        "empty, or rate limited.",
    )


def _embedded_post(plain: str, name: str) -> dict[str, Any] | None:
    """Extract the newest post from the escaped Relay block on a profile page."""

    block_start = plain.find('"__isTweetResult":"Tweet"')
    if block_start < 0:
        return None
    # The tweet's own id sits immediately before its result object.
    head = plain[:block_start]
    tweet = None
    for candidate in re.finditer(r'"rest_id":"(\d{10,25})","result":\{', head):
        tweet = candidate
    text_match = re.search(
        r'"full_text":"(.*?)","hashtag_entities"', plain[block_start:], re.S
    )
    if not text_match:
        return None
    text = text_match.group(1)
    if text.endswith("\\"):
        text = text[:-1]
    block = plain[block_start:]
    author = re.search(r'"result":\{"__typename":"User".*?"screen_name":"(.*?)"', block, re.S)
    created = re.search(r'"created_at_ms":(\d{10,14})', block)
    counts = re.search(
        r'"counts":\{"bookmark_count":\d+,"favorite_count":(\d+),"quote_count":(\d+),'
        r'"reply_count":(\d+),"retweet_count":(\d+)\}',
        block,
    )
    conversation = re.search(r'"conversation_id":"(\d{10,25})"', block)

    created_at = None
    if created:
        try:
            created_at = time.strftime(
                "%a %b %d %H:%M:%S +0000 %Y", time.gmtime(int(created.group(1)) / 1000)
            )
        except (OverflowError, ValueError):
            created_at = None
    post_id = tweet.group(1) if tweet else None
    if not post_id:
        return None
    screen_name = author.group(1) if author else name
    return normalize_post(
        {
            "id_str": post_id,
            "full_text": _unescape(text),
            "created_at": created_at,
            "conversation_id_str": conversation.group(1) if conversation else post_id,
            "favorite_count": int(counts.group(1)) if counts else None,
            "quote_count": int(counts.group(2)) if counts else None,
            "reply_count": int(counts.group(3)) if counts else None,
            "retweet_count": int(counts.group(4)) if counts else None,
            "permalink": f"/{screen_name}/status/{post_id}",
            "user": {"screen_name": screen_name},
        }
    )


@dataclass
class _CacheEntry:
    expires_at: float
    value: Any


def _default_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:  # pragma: no cover - defensive
            body = b""
        return int(exc.code), body or b""
    except urllib.error.URLError as exc:
        raise PublicReadError(
            "PUBLIC_READ_NETWORK",
            "The public X page could not be reached.",
            retryable=True,
        ) from exc


class PublicXClient:
    """Read-only client with a TTL cache, a small interval, and no auth.

    ``transport`` exists so tests can run offline. The default transport sends
    no cookie, no bearer token, and no user identifier.
    """

    def __init__(
        self,
        *,
        transport: Callable[[str, dict[str, str], float], tuple[int, bytes]] | None = None,
        timeout: float = 10.0,
        cache_ttl: float = 90.0,
        min_interval: float = 0.6,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.transport = transport or _default_transport
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.min_interval = min_interval
        self._now = now or time.monotonic
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()
        self._last_call = 0.0
        self.stats = {"requests": 0, "cache_hits": 0, "failures": 0}

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (self._now() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = self._now()

    def _fetch(self, url: str) -> str:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json;q=0.9",
            "Accept-Language": "en-US,en;q=0.9",
        }
        attempt = 0
        while True:
            attempt += 1
            self._throttle()
            try:
                status, body = self.transport(url, headers, self.timeout)
            except PublicReadError as exc:
                if not exc.retryable or attempt >= 3:
                    self.stats["failures"] += 1
                    raise
                time.sleep(0.3 * attempt + random.random() * 0.2)
                continue
            self.stats["requests"] += 1
            if status == 200 and body:
                return body.decode("utf-8", "replace")
            if status in {429, 500, 502, 503, 504} and attempt < 3:
                time.sleep(0.4 * attempt + random.random() * 0.3)
                continue
            self.stats["failures"] += 1
            code = "PUBLIC_READ_RATE_LIMITED" if status == 429 else "PUBLIC_READ_HTTP"
            raise PublicReadError(
                code,
                f"X returned HTTP {status} for a public page.",
                retryable=status in {429, 500, 502, 503, 504},
                status=status,
            )

    def _cached(self, key: str, loader: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
        now = self._now()
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and entry.expires_at > now:
                self.stats["cache_hits"] += 1
                age = max(0.0, self.cache_ttl - (entry.expires_at - now))
                return entry.value, {"hit": True, "age_seconds": round(age, 1)}
        value = loader()
        with self._lock:
            self._cache[key] = _CacheEntry(expires_at=now + self.cache_ttl, value=value)
        return value, {"hit": False, "age_seconds": 0.0}

    def _envelope(self, url: str, cache: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {
            "source": "x_public_web",
            "url": url,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cache": cache,
            "credential": "none",
            "disclosure": (
                "LIVE public page read. No OAuth, API key, cookie, or X developer app "
                "was used. Public pages can change format without notice."
            ),
            **extra,
        }

    def get_profile(self, screen_name: str) -> dict[str, Any]:
        name = _validate_screen_name(screen_name)
        url = PROFILE_URL.format(screen_name=name)
        def load() -> dict[str, Any]:
            try:
                return extract_profile(self._fetch(url), name)
            except PublicReadError as exc:
                if exc.status == 404:
                    raise PublicReadError(
                        "PROFILE_NOT_FOUND",
                        f"X has no public profile at @{name}.",
                        status=404,
                    ) from None
                raise

        profile, cache = self._cached(f"profile:{name.lower()}", load)
        return self._envelope(url, cache, profile=profile)

    def get_timeline(self, screen_name: str, limit: int = 20) -> dict[str, Any]:
        name = _validate_screen_name(screen_name)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise PublicReadError(
                "INVALID_ARGUMENT", "limit must be an integer from 1 to 50."
            )
        url = TIMELINE_URL.format(screen_name=name)

        def load() -> list[dict[str, Any]]:
            try:
                posts = extract_timeline(self._fetch(url), name, limit)
            except PublicReadError as exc:
                if exc.code != "TIMELINE_NOT_PARSED":
                    raise
                # The embed endpoint can return an empty shell; the profile page
                # still carries the newest post.
                return extract_timeline(
                    self._fetch(PROFILE_URL.format(screen_name=name)), name, limit
                )
            if posts:
                return posts
            # The embed can parse cleanly yet contain no entries. Try the
            # profile page before reporting an empty timeline.
            try:
                return extract_timeline(
                    self._fetch(PROFILE_URL.format(screen_name=name)), name, limit
                )
            except PublicReadError:
                return []

        posts, cache = self._cached(f"timeline:{name.lower()}:{limit}", load)
        return self._envelope(url, cache, screen_name=name, posts=list(posts)[:limit])

    def get_post(self, post_ref: str) -> dict[str, Any]:
        post_id = _validate_post_id(post_ref)
        url = POST_URL.format(post_id=post_id)

        def load() -> dict[str, Any]:
            page = self._fetch(url)
            try:
                tweet = json.loads(page)
            except json.JSONDecodeError as exc:
                raise PublicReadError(
                    "POST_NOT_PARSED", "The public post endpoint returned invalid JSON."
                ) from exc
            if not isinstance(tweet, dict) or not tweet.get("text"):
                raise PublicReadError(
                    "POST_NOT_FOUND",
                    "The post is not available through the public endpoint. It may be "
                    "deleted, protected, or age-restricted.",
                )
            return normalize_post(tweet)

        post, cache = self._cached(f"post:{post_id}", load)
        return self._envelope(url, cache, post=post)
