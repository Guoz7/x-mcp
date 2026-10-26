"""Derive X read results from visible browser state.

The X MCP never trusts a caller's prose. A read result is built from what the
executor observed on the page, then checked against the same schema a manual
executor submission must satisfy.

Two reads are supported:

* ``observe_account`` - the signed-in owner's own profile.
* ``observe_timeline`` - posts visible on the home timeline.

Both fail closed. If the page is a login wall, has no articles, or lacks the
fields the schema requires, the caller gets a typed error instead of a guess.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from browser_executor import (
    Browser,
    ExecutorError,
    X_RESERVED_PATHS,
    _profile_screen_name,
    _visible_links,
)


class ReadError(Exception):
    """A read failure that is safe to return through MCP."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


SIGNED_OUT_URL_MARKERS = (
    "/i/jf/onboarding",
    "redirect_after_login",
    "/i/flow/login",
    "/login",
    "/i/flow/signup",
)

DEFAULT_BASE_URL = "https://x.com"


def _base_url(value: str | None) -> str:
    """Resolve the timeline origin.

    Production always uses x.com. Tests point this at a loopback fixture so
    the real parsing and failure paths run end to end. The executor allowlist
    still gates every navigation, so this cannot widen reach by itself.
    """

    if not value:
        return DEFAULT_BASE_URL
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ReadError("INVALID_BASE_URL", "base_url must be an absolute http(s) URL.")
    return f"{parsed.scheme}://{parsed.netloc}"


def _hosts_for(base: str) -> frozenset[str]:
    """Which hosts count as the same site as the base URL."""

    host = (urlparse(base).hostname or "").lower()
    if host in X_HOSTS:
        return X_HOSTS
    return frozenset({host})

# Localized "N posts" counters. X renders the count in the page language, so a
# single English pattern would silently report 0 for a zh-CN account.
POST_COUNT_PATTERNS = (
    r"(?<!\d)(\d{1,3}(?:[,.]\d{3})+|\d+)\s*(?:posts?|Posts?|POSTS?)\b",
    r"(?<!\d)(\d{1,3}(?:[,.]\d{3})+|\d+)\s*帖子",
    r"(?<!\d)(\d{1,3}(?:[,.]\d{3})+|\d+)\s*ポスト",
    r"(?<!\d)(\d{1,3}(?:[,.]\d{3})+|\d+)\s*Beiträge",
    r"(?<!\d)(\d{1,3}(?:[,.]\d{3})+|\d+)\s*publicaciones",
    r"(?<!\d)(\d{1,3}(?:[,.]\d{3})+|\d+)\s*publications",
    r"(?<!\d)(\d{1,3}(?:[,.]\d{3})+|\d+)\s*টি\s*পোস্ট",
)

# X abbreviates large counters ("12.5K posts"). The exact number is not on the
# page, so the caller must see that the value is an estimate.
ABBREVIATED_POST_COUNT = (
    r"(?<!\d)(\d+(?:[.,]\d+)?)\s*([KM])\s*(?:posts?|Posts?|帖子|ポスト|Beiträge|publicaciones|publications)\b"
)


def _looks_signed_out(url: str) -> bool:
    return any(marker in url for marker in SIGNED_OUT_URL_MARKERS)


X_HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})


def _handle_from_status_url(
    url: str, hosts: frozenset[str] | set[str] = X_HOSTS
) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.hostname not in hosts:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 3:
        return None
    handle, marker, status_id = parts
    if marker != "status" or not status_id.isdigit():
        return None
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return None
    return handle, status_id


def _profile_candidates(
    observation: dict[str, Any], hosts: frozenset[str] | set[str] = X_HOSTS
) -> list[str]:
    """Handles that appear as a single-segment link, in page order."""

    candidates: list[str] = []
    for link in observation.get("links") or []:
        parsed = urlparse(str(link))
        if parsed.hostname not in hosts:
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1:
            continue
        handle = parts[0]
        if handle.lower() in X_RESERVED_PATHS:
            continue
        if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle) and handle not in candidates:
            candidates.append(handle)
    return candidates


def _own_handle(
    observation: dict[str, Any], hosts: frozenset[str] | set[str] = X_HOSTS
) -> str:
    """Pick the signed-in handle from the left navigation profile link.

    Only ``a[data-testid="AppTabBar_Profile_Link"]`` identifies the account that
    is actually signed in. Earlier versions also accepted a post author from the
    visible timeline, which silently returned other people's accounts as the
    owner. That is a write-safety bug: an action could be prepared against the
    wrong account. Do not reintroduce that heuristic.
    """

    raw = observation.get("nav_profile_href")
    handle = ""
    if isinstance(raw, str) and raw:
        parts = [part for part in urlparse(raw).path.split("/") if part]
        if len(parts) == 1:
            handle = parts[0]
    if not handle or handle.lower() in X_RESERVED_PATHS:
        raise ReadError(
            "OWNER_HANDLE_NOT_VISIBLE",
            (
                "The left navigation did not expose the signed-in profile link. "
                "Open x.com/home while signed in, or the session is not authenticated."
            ),
        )
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        raise ReadError(
            "OWNER_HANDLE_NOT_VISIBLE",
            "The navigation profile link did not contain a valid screen name.",
        )
    return handle


def _detect_login_wall(observation: dict[str, Any]) -> None:
    """Fail when the page is a login wall, even if the URL still looks normal.

    X sometimes renders the sign-in form in place instead of redirecting, so a
    URL-only check would report an empty timeline for a signed-out profile.
    """

    if _looks_signed_out(observation.get("url", "")):
        raise ReadError(
            "OWNER_SIGNED_OUT",
            "X redirected to a login wall. The owner profile is not signed in.",
            details={"url_path": observation.get("url", "").split("?")[0][:120]},
        )
    links = {str(link) for link in observation.get("links") or []}
    login_links = {
        link for link in links
        if urlparse(link).path.rstrip("/").endswith(("/i/flow/login", "/login"))
    }
    if login_links and not observation.get("articles"):
        raise ReadError(
            "OWNER_SIGNED_OUT",
            "The page shows only sign-in navigation and no posts.",
            details={"login_links": len(login_links)},
        )


def _parse_post_count(text: str) -> tuple[int, str] | None:
    for pattern in POST_COUNT_PATTERNS:
        match = re.search(pattern, text)
        if not match:
            continue
        raw = match.group(1).replace(",", "").replace(".", "").replace(" ", "")
        if raw.isdigit():
            return int(raw), "parsed_from_page"
    abbreviated = re.search(ABBREVIATED_POST_COUNT, text)
    if abbreviated:
        value = float(abbreviated.group(1).replace(",", "."))
        scale = 1000 if abbreviated.group(2) == "K" else 1_000_000
        # Round down: never overstate what the page showed.
        return int(value * scale), "abbreviated_estimate"
    return None


def read_account(browser: Browser, *, base_url: str | None = None) -> dict[str, Any]:
    """Read the signed-in owner's profile from visible page state."""

    base = _base_url(base_url)
    try:
        home = browser.open(f"{base}/home")
    except ExecutorError as exc:
        raise ReadError(exc.code, exc.message) from None
    _detect_login_wall(home)
    handle = _own_handle(home, _hosts_for(base))
    try:
        profile = browser.open(f"{base}/{handle}")
    except ExecutorError as exc:
        raise ReadError(exc.code, exc.message) from None
    _detect_login_wall(profile)
    projection = profile.get("profile") or {}
    if not projection.get("user_id"):
        raise ReadError(
            "ACCOUNT_ID_NOT_VISIBLE",
            "The numeric account id was not present in the profile page metadata.",
            details={"screen_name": handle},
        )
    parsed = _parse_post_count(profile.get("text", ""))
    recent = list(projection.get("recent_post_ids") or [])
    if parsed is not None:
        posts_count, count_source = parsed
    else:
        # Fall back to what is actually on screen and label it, rather than
        # inventing a total the page never showed.
        posts_count, count_source = len(recent), "visible_article_count"
    lines = [line.strip() for line in profile.get("text", "").splitlines() if line.strip()]
    display_name = lines[0] if lines else handle
    return {
        "username": projection.get("screen_name") or handle,
        "user_id": str(projection["user_id"]),
        "display_name": display_name[:80],
        "posts_count": posts_count,
        "recent_post_ids": recent[:20],
        "posts_count_source": count_source,
        "profile_url": f"{base}/{handle}",
    }


def read_timeline(
    browser: Browser, *, limit: int = 20, base_url: str | None = None
) -> dict[str, Any]:
    """Read canonical posts from the visible home timeline.

    X virtualizes the feed, so one observation often exposes fewer posts than
    the requested limit. Scroll through a bounded number of observations and
    merge the visible results. The browser still returns only page evidence.
    """

    base = _base_url(base_url)
    try:
        home = browser.open(f"{base}/home")
    except ExecutorError as exc:
        raise ReadError(exc.code, exc.message) from None
    _detect_login_wall(home)
    observations = 0
    scrolls = 0
    seen_posts: set[str] = set()
    posts: list[dict[str, Any]] = []
    skipped_without_link = 0

    while True:
        observations += 1
        articles = home.get("articles") or []
        if not articles and not posts:
            raise ReadError(
                "TIMELINE_EMPTY",
                "No posts were visible on the home timeline.",
                details={"url_path": home["url"].split("?")[0][:120]},
            )

        for article in articles:
            text = str(article.get("text", "")).strip()
            status = None
            for link in article.get("links") or []:
                status = _handle_from_status_url(str(link), _hosts_for(base))
                if status:
                    break
            if status is None:
                skipped_without_link += 1
                continue
            author, status_id = status
            post_url = f"{base}/{author}/status/{status_id}"
            if not text or post_url in seen_posts:
                continue
            seen_posts.add(post_url)
            posts.append({"url": post_url, "author": author, "text": text})
            if len(posts) >= limit:
                break
        if len(posts) >= limit:
            break

        # Keep the read bounded. If a mock or an older executor does not
        # expose observation-bound scrolling, return the evidence already seen.
        if scrolls >= 20:
            break
        observation_id = home.get("observation_id")
        fingerprint = home.get("fingerprint_sha256")
        if not isinstance(observation_id, str) or not isinstance(fingerprint, str):
            break
        try:
            next_home = browser.scroll(
                observation_id=observation_id,
                fingerprint_sha256=fingerprint,
                delta_y=900,
            )
        except ExecutorError:
            break
        if not isinstance(next_home, dict):
            break
        home = next_home
        scrolls += 1

    if not posts:
        raise ReadError(
            "TIMELINE_LINKS_MISSING",
            "Visible posts had no canonical status permalink.",
            details={"articles": len(home.get("articles") or []), "skipped": skipped_without_link},
        )
    return {
        "posts": posts[:limit],
        "visible_articles": len(home.get("articles") or []),
        "observations": observations,
        "scrolls": scrolls,
        "skipped_without_permalink": skipped_without_link,
    }


def executor_context(browser: Browser) -> dict[str, Any]:
    """Safe metadata about the live context, for action completion."""

    observation = browser.observe()
    context: dict[str, Any] = {
        "browser_family": observation.get("browser_family", "chromium"),
        "os": "windows",
        "viewport": {
            "width": observation["width"],
            "height": observation["height"],
        },
        "device_pixel_ratio": observation.get("dpr", 1),
        "locale": observation.get("locale"),
        "color_scheme": observation.get("color_scheme"),
        "tab": {"url": observation["url"][:300], "title": observation["title"][:120]},
    }
    return {key: value for key, value in context.items() if value is not None}
