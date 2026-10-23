"""Small local browser executor for controlled DSH smoke tests.

The executor owns a fresh Chromium profile and exposes only visible-page
operations. It never reads cookies, local storage, passwords, or browser DBs.
It is intentionally separate from the X MCP action queue: the X MCP decides
what may happen, while this process performs a bounded browser observation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


HOST = "127.0.0.1"
PORT = int(os.environ.get("X_BROWSER_EXECUTOR_PORT", "8770"))
PROFILE = Path(os.environ.get("X_BROWSER_EXECUTOR_PROFILE", ".browser-executor-profile")).resolve()
CHROME = os.environ.get(
    "X_BROWSER_EXECUTOR_CHROME",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)
ALLOWED_HOSTS = {
    item.strip().lower()
    for item in os.environ.get(
        "X_BROWSER_EXECUTOR_ALLOWED_HOSTS", "google.com,www.google.com,x.com,www.x.com,twitter.com"
    ).split(",")
    if item.strip()
}
AUDIT_PATH = Path(os.environ.get("X_BROWSER_EXECUTOR_AUDIT", ".browser-executor-audit.jsonl")).resolve()
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

# Context knobs that change how X renders. They are read once per browser
# start, because Playwright fixes them for the lifetime of a context.
SUPPORTED_COLOR_SCHEMES = frozenset({"light", "dark", "no-preference"})
SUPPORTED_LOCALES = frozenset(
    {
        "en-US", "en-GB", "zh-CN", "zh-TW", "ja-JP", "ko-KR",
        "de-DE", "fr-FR", "es-ES", "pt-BR", "ru-RU", "ar-SA",
    }
)
MAX_VISIBLE_LINKS = 60
MAX_ARTICLES = 40
MAX_ARTICLE_TEXT = 1500
X_RESERVED_PATHS = frozenset(
    {
        "home", "explore", "notifications", "messages", "i", "search",
        "settings", "compose", "login", "logout", "signup", "jobs",
        "premium", "about", "tos", "privacy", "help", "download",
    }
)


def _user_agent() -> str:
    """Headless Chromium advertises "HeadlessChrome", which X rejects with 403.

    A normal browser UA is required for any real x.com navigation. Callers can
    still override it for deliberate compatibility testing.
    """

    return os.environ.get("X_BROWSER_EXECUTOR_USER_AGENT", "").strip() or DEFAULT_USER_AGENT


def _viewport() -> dict[str, int]:
    try:
        width = int(os.environ.get("X_BROWSER_EXECUTOR_WIDTH", "1280"))
        height = int(os.environ.get("X_BROWSER_EXECUTOR_HEIGHT", "720"))
    except ValueError as exc:
        raise ExecutorError(
            "INVALID_ARGUMENT", "Viewport width and height must be integers."
        ) from exc
    if not 160 <= width <= 10000 or not 160 <= height <= 10000:
        raise ExecutorError(
            "INVALID_ARGUMENT", "Viewport width and height must be from 160 to 10000."
        )
    return {"width": width, "height": height}


def _device_scale_factor() -> float:
    raw = os.environ.get("X_BROWSER_EXECUTOR_DEVICE_SCALE_FACTOR", "1").strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ExecutorError(
            "INVALID_ARGUMENT", "X_BROWSER_EXECUTOR_DEVICE_SCALE_FACTOR must be a number."
        ) from exc
    if not 0.5 <= value <= 8:
        raise ExecutorError(
            "INVALID_ARGUMENT", "Device scale factor must be from 0.5 to 8."
        )
    return value


def _locale() -> str:
    value = os.environ.get("X_BROWSER_EXECUTOR_LOCALE", "en-US").strip() or "en-US"
    if value not in SUPPORTED_LOCALES:
        raise ExecutorError(
            "INVALID_ARGUMENT",
            "Unsupported locale. Pick one of: " + ", ".join(sorted(SUPPORTED_LOCALES)),
        )
    return value


def _color_scheme() -> str | None:
    value = os.environ.get("X_BROWSER_EXECUTOR_COLOR_SCHEME", "").strip().lower()
    if not value:
        return None
    if value not in SUPPORTED_COLOR_SCHEMES:
        raise ExecutorError(
            "INVALID_ARGUMENT", "Color scheme must be light, dark, or no-preference."
        )
    return value


def _viewport_class(width: int) -> str:
    """Classify the live viewport with the same bands the bridge advertises."""

    if width < 768:
        return "mobile"
    if width < 1024:
        return "tablet"
    return "desktop"


def _visible_links(raw: Any) -> list[str]:
    """Keep the hrefs a human could see, without query strings or fragments.

    A timeline read needs post permalinks, and innerText does not contain them.
    Only same-allowlist http(s) anchors survive; duplicates collapse and the
    list stays bounded so one page cannot flood the tool result.
    """

    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or len(value) > 300:
            continue
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if not _allowed(clean):
            continue
        if clean not in seen:
            seen.append(clean)
        if len(seen) >= MAX_VISIBLE_LINKS:
            break
    return seen


def _visible_articles(raw: Any) -> list[dict[str, Any]]:
    """Keep bounded per-item text so a timeline can be read as items.

    X renders each post in an <article>. Returning the whole body text alone
    makes it impossible to attribute a permalink to a specific post, so the
    observation carries one bounded record per article instead.
    """

    if not isinstance(raw, list):
        return []
    articles: list[dict[str, Any]] = []
    for item in raw[:MAX_ARTICLES]:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue
        links = _visible_links(item.get("links"))
        articles.append({"text": text[:MAX_ARTICLE_TEXT], "links": links})
    return articles


def _profile_hosts() -> frozenset[str]:
    raw = os.environ.get("X_BROWSER_EXECUTOR_PROFILE_HOSTS", "").strip()
    if not raw:
        return frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})
    return frozenset(item.strip().lower() for item in raw.split(",") if item.strip())


def _profile_screen_name(url: str, hosts: frozenset[str] | None = None) -> str | None:
    """Return the handle when the current URL is a profile page, else None."""

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed = _profile_hosts() if hosts is None else hosts
    if host not in allowed:
        return None
    segments = [part for part in parsed.path.split("/") if part]
    if len(segments) != 1:
        return None
    handle = segments[0]
    if handle.lower() in X_RESERVED_PATHS:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return None
    return handle


def _user_id_from_html(html: str, handle: str) -> str | None:
    """Read the numeric account id that X embeds in profile media URLs.

    X does not put the id in visible text, and scraping arbitrary JSON blobs
    would be fragile. Profile banner and avatar URLs carry it as a path
    segment, which is stable and unambiguous for the page it appears on.
    """

    for pattern in (
        r"profile_banners/(\d{5,25})/",
        r"profile_images/(\d{5,25})/",
    ):
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


def _audit(event: dict[str, Any]) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": time.time(), **event}, ensure_ascii=False) + "\n")


class ExecutorError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ALLOWED_HOSTS or any(host.endswith("." + item) for item in ALLOWED_HOSTS)


class Browser:
    def __init__(self, *, profile: Path | None = None) -> None:
        self.profile = PROFILE if profile is None else profile.resolve()
        self.persistent = os.environ.get("X_BROWSER_EXECUTOR_PERSISTENT", "1") != "0"
        self.start_timeout = float(os.environ.get("X_BROWSER_EXECUTOR_START_TIMEOUT", "90"))
        self.timed_out = False
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self.observation_id: str | None = None
        self.fingerprint: str | None = None
        self.observed_at = 0.0
        self.visual_observation_id: str | None = None
        self.visual_digest: str | None = None
        self.x_grant: dict[str, Any] | None = None
        self.lock = threading.RLock()
        self.tasks: queue.Queue[Any] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.worker_ident: int | None = None

    def _ensure_worker(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        ready = threading.Event()

        def run() -> None:
            self.worker_ident = threading.get_ident()
            ready.set()
            while True:
                item = self.tasks.get()
                if item is None:
                    return
                function, done, box = item
                try:
                    box["result"] = function()
                except BaseException as exc:
                    box["error"] = exc
                finally:
                    done.set()
                    # The abandoned caller already reported a timeout. Now that
                    # this task is no longer running, new calls can proceed.
                    self.timed_out = False

        self.worker = threading.Thread(target=run, name="browser-executor-worker", daemon=True)
        self.worker.start()
        ready.wait(3)

    def _run_on_worker(self, function: Any, timeout: float = 30, allow_stuck: bool = False) -> Any:
        self._ensure_worker()
        if self.timed_out and not allow_stuck:
            raise ExecutorError(
                "BROWSER_EXECUTOR_STUCK",
                "A previous browser task is still running. Close this browser session.",
            )
        if threading.get_ident() == self.worker_ident:
            return function()
        done = threading.Event()
        box: dict[str, Any] = {}
        self.tasks.put((function, done, box))
        if not done.wait(timeout):
            # The queued call may still complete later. Mark the executor so the
            # owner can discard it instead of queueing more work behind it.
            self.timed_out = True
            raise ExecutorError("BROWSER_EXECUTOR_TIMEOUT", "The browser executor worker timed out.")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def open(self, url: str) -> dict[str, Any]:
        if not _allowed(url):
            raise ExecutorError("HOST_NOT_ALLOWED", "The requested host is outside the executor allowlist.")
        # Chromium cold start is much slower than a page interaction.
        return self._run_on_worker(lambda: self._open_impl(url), timeout=self.start_timeout)

    def _open_impl(self, url: str) -> dict[str, Any]:
        with self.lock:
            proxy = os.environ.get("X_BROWSER_EXECUTOR_PROXY", "").strip()
            try:
                from playwright.sync_api import Error as PlaywrightError
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise ExecutorError(
                    "BROWSER_RUNTIME_MISSING",
                    "Run the executor with the bundled Playwright environment.",
                ) from exc
            if self.context is None:
                self.profile.mkdir(parents=True, exist_ok=True)
                self.playwright = sync_playwright().start()
                try:
                    browser_options = {
                        "executable_path": CHROME,
                        "headless": os.environ.get("X_BROWSER_EXECUTOR_HEADLESS") == "1",
                        "viewport": _viewport(),
                        "args": ["--disable-sync", "--no-first-run", "--no-default-browser-check"],
                        "user_agent": _user_agent(),
                    }
                    context_options: dict[str, Any] = {
                        "locale": _locale(),
                        "device_scale_factor": _device_scale_factor(),
                    }
                    color_scheme = _color_scheme()
                    if color_scheme is not None:
                        context_options["color_scheme"] = color_scheme
                    if proxy:
                        browser_options["proxy"] = {"server": proxy}
                    if self.persistent:
                        self.context = self.playwright.chromium.launch_persistent_context(
                            user_data_dir=str(self.profile),
                            **browser_options,
                            **context_options,
                        )
                    else:
                        viewport = browser_options.pop("viewport")
                        user_agent = browser_options.pop("user_agent")
                        self.browser = self.playwright.chromium.launch(**browser_options)
                        self.context = self.browser.new_context(
                            viewport=viewport, user_agent=user_agent, **context_options
                        )
                    self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
                    self.page.goto(url, wait_until="commit", timeout=15000)
                    self.page.wait_for_timeout(1200)
                except PlaywrightError as exc:
                    self.close()
                    raise ExecutorError("BROWSER_START_FAILED", str(exc)) from exc
            else:
                self.page.goto(url, wait_until="commit", timeout=15000)
                self.page.wait_for_timeout(1200)
            return self.observe()

    def observe(self) -> dict[str, Any]:
        return self._run_on_worker(self._observe_impl)

    def _observe_impl(self) -> dict[str, Any]:
        if self.page is None:
            raise ExecutorError("BROWSER_NOT_OPEN", "Open a browser before observing.")
        result = self.page.evaluate(
            """() => ({title: document.title, url: location.href,
            text: (document.body && document.body.innerText || '').slice(0, 12000),
            width: innerWidth, height: innerHeight, dpr: devicePixelRatio,
            outer_width: outerWidth, outer_height: outerHeight,
            screen_width: screen.width, screen_height: screen.height,
            visual_scale: (window.visualViewport && visualViewport.scale) || 1,
            locale: navigator.language,
            color_scheme: (matchMedia('(prefers-color-scheme: dark)').matches
                ? 'dark' : 'light'),
            links: Array.from(document.querySelectorAll('a[href]'))
                .slice(0, 400)
                .map(a => a.href)
                .filter(href => typeof href === 'string' && href.length < 300),
            articles: Array.from(document.querySelectorAll('article'))
                .slice(0, 40)
                .map(node => ({
                    text: (node.innerText || '').slice(0, 1500),
                    links: Array.from(node.querySelectorAll('a[href]'))
                        .slice(0, 20)
                        .map(a => a.href)
                        .filter(href => typeof href === 'string' && href.length < 300),
                })),
            scrollX, scrollY, ready: document.readyState})"""
        )
        if not _allowed(result["url"]):
            raise ExecutorError("HOST_NOT_ALLOWED", "The page navigated outside the executor allowlist.")
        result["links"] = _visible_links(result.get("links"))
        result["articles"] = _visible_articles(result.get("articles"))
        result["article_count"] = len(result["articles"])

        handle = _profile_screen_name(result["url"])
        if handle:
            # Only profile pages pay for this read. The HTML stays inside the
            # worker; the caller receives validated fields, never the source.
            html = self.page.evaluate("document.documentElement.outerHTML")
            status_ids: list[str] = []
            for article in result["articles"]:
                for link in article["links"]:
                    parts = [p for p in urlparse(link).path.split("/") if p]
                    if len(parts) == 3 and parts[0].lower() == handle.lower() and parts[1] == "status":
                        if parts[2].isdigit() and parts[2] not in status_ids:
                            status_ids.append(parts[2])
            result["profile"] = {
                "screen_name": handle,
                "user_id": _user_id_from_html(html, handle),
                "recent_post_ids": status_ids[:20],
                "profile_url": f"https://x.com/{handle}",
            }
        # Locale and color scheme are part of the fingerprint: both change what
        # the page renders, so a cached observation must not survive a flip.
        fingerprint_data = {
            key: result.get(key)
            for key in (
                "url", "width", "height", "dpr", "scrollX", "scrollY",
                "ready", "locale", "color_scheme",
            )
        }
        fingerprint = hashlib.sha256(_json(fingerprint_data)).hexdigest()
        self.observation_id = "obs_" + secrets.token_hex(8)
        self.fingerprint = fingerprint
        self.observed_at = time.monotonic()
        return {
            "observation_id": self.observation_id,
            "fingerprint_sha256": fingerprint,
            "browser_family": "chromium",
            "viewport_class": _viewport_class(result["width"]),
            **result,
        }

    def screenshot(
        self, observation_id: str, fingerprint_sha256: str, include_data: bool = False
    ) -> dict[str, Any]:
        return self._run_on_worker(
            lambda: self._screenshot_impl(observation_id, fingerprint_sha256, include_data)
        )

    def _screenshot_impl(
        self, observation_id: str, fingerprint_sha256: str, include_data: bool
    ) -> dict[str, Any]:
        self._assert_observation(observation_id, fingerprint_sha256)
        image = self.page.screenshot(type="png", full_page=False)
        self.visual_observation_id = observation_id
        self.visual_digest = hashlib.sha256(image).hexdigest()
        result = {
            "observation_id": observation_id,
            "fingerprint_sha256": fingerprint_sha256,
            "mime_type": "image/png",
            "width": self.page.evaluate("innerWidth"),
            "height": self.page.evaluate("innerHeight"),
            "bytes": len(image),
            "sha256": hashlib.sha256(image).hexdigest(),
        }
        if include_data:
            result["data_base64"] = base64.b64encode(image).decode("ascii")
        return result

    def click(
        self,
        *,
        observation_id: str,
        fingerprint_sha256: str,
        selector: str | None = None,
        x_ratio: float | None = None,
        y_ratio: float | None = None,
    ) -> dict[str, Any]:
        return self._run_on_worker(
            lambda: self._click_impl(
                observation_id, fingerprint_sha256, selector, x_ratio, y_ratio
            )
        )

    def scroll(self, *, observation_id: str, fingerprint_sha256: str,
               delta_y: int) -> dict[str, Any]:
        if isinstance(delta_y, bool) or not isinstance(delta_y, int) or not -2000 <= delta_y <= 2000:
            raise ExecutorError("INVALID_ARGUMENT", "delta_y must be an integer between -2000 and 2000.")
        return self._run_on_worker(
            lambda: self._scroll_impl(observation_id, fingerprint_sha256, delta_y)
        )

    def _scroll_impl(self, observation_id: str, fingerprint_sha256: str,
                     delta_y: int) -> dict[str, Any]:
        self._assert_observation(observation_id, fingerprint_sha256)
        self.observation_id = None
        self.visual_observation_id = None
        self.visual_digest = None
        try:
            # Scroll the document without keyboard shortcuts or arbitrary scripts.
            self.page.evaluate("delta => window.scrollBy({top: delta, left: 0, behavior: 'instant'})", delta_y)
            self.page.wait_for_timeout(300)
            return self._observe_impl()
        except Exception as exc:
            raise ExecutorError("ACTION_UNCERTAIN", "Scroll verification failed; observe again before retrying.") from exc

    def type_text(self, *, observation_id: str, fingerprint_sha256: str,
                  text: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text or len(text) > 280:
            raise ExecutorError(
                "INVALID_ARGUMENT", "text must be a string of 1 to 280 characters."
            )
        return self._run_on_worker(
            lambda: self._type_impl(observation_id, fingerprint_sha256, text)
        )

    def _type_impl(self, observation_id: str, fingerprint_sha256: str,
                   text: str) -> dict[str, Any]:
        self._assert_observation(observation_id, fingerprint_sha256)
        host = (urlparse(self.page.url).hostname or "").lower()
        if host in {"x.com", "twitter.com"} or host.endswith((".x.com", ".twitter.com")):
            # Typing on X is a write step and needs the same claim as a click.
            self._consume_x_grant(self.X_TYPE_ACTIONS)
        self.observation_id = None
        self.visual_observation_id = None
        self.visual_digest = None
        try:
            self.page.keyboard.type(text, delay=0)
            self.page.wait_for_timeout(200)
            return self._observe_impl()
        except Exception as exc:
            raise ExecutorError(
                "ACTION_UNCERTAIN", "Typing failed; observe again before retrying."
            ) from exc

    def _assert_observation(self, observation_id: str, fingerprint_sha256: str) -> None:
        if observation_id != self.observation_id or fingerprint_sha256 != self.fingerprint:
            raise ExecutorError("OBSERVATION_STALE", "The browser observation is stale.")
        if time.monotonic() - self.observed_at > 120:
            self.observation_id = None
            raise ExecutorError("OBSERVATION_STALE", "The browser observation expired.")
        # Re-read the live page, not just the caller's cached fingerprint.
        current = self._observe_impl()
        if current["fingerprint_sha256"] != fingerprint_sha256:
            raise ExecutorError("OBSERVATION_STALE", "The visible page state changed.")
        self.observation_id = observation_id

    def _click_impl(
        self,
        observation_id: str,
        fingerprint_sha256: str,
        selector: str | None,
        x_ratio: float | None,
        y_ratio: float | None,
    ) -> dict[str, Any]:
        self._assert_observation(observation_id, fingerprint_sha256)
        host = (urlparse(self.page.url).hostname or "").lower()
        if host in {"x.com", "twitter.com"} or host.endswith((".x.com", ".twitter.com")):
            # X interaction requires a live, single-use claim from the action queue.
            self._consume_x_grant(self.X_CLICK_ACTIONS)
        if selector:
            target = self.page.locator(selector)
            if target.count() != 1:
                raise ExecutorError("TARGET_NOT_UNIQUE", "The selector must match exactly one element.")
        elif x_ratio is not None and y_ratio is not None:
            if not (0 <= x_ratio <= 1 and 0 <= y_ratio <= 1):
                raise ExecutorError("INVALID_COORDINATE", "Coordinate ratios must be between 0 and 1.")
            self._assert_visual_snapshot(observation_id)
        else:
            raise ExecutorError("INVALID_ARGUMENT", "Provide selector or coordinate ratios.")
        # Consume before dispatch: a timeout may happen after the click landed.
        self.observation_id = None
        try:
            return self._dispatch_click(selector, x_ratio, y_ratio)
        except Exception as exc:
            raise ExecutorError(
                "ACTION_UNCERTAIN", "Click dispatch or verification failed; do not retry this observation."
            ) from exc

    # Only these claimed action types may touch a signed-in X page.
    X_TYPE_ACTIONS = frozenset({"publish_post"})
    X_CLICK_ACTIONS = frozenset({"publish_post", "delete_post"})

    def authorize_x_action(
        self,
        *,
        action_id: str,
        claim_token: str,
        lease_expires_epoch: float,
        action_type: str = "",
    ) -> dict[str, Any]:
        """Arm exactly one X interaction for a claimed, confirmed action."""

        if not action_id or not claim_token:
            raise ExecutorError("INVALID_ARGUMENT", "A claimed action is required.")
        if action_type not in self.X_CLICK_ACTIONS:
            raise ExecutorError(
                "X_ACTION_NOT_A_WRITE",
                "Only a claimed publish or delete action may touch a signed-in X page.",
            )
        if lease_expires_epoch <= time.time():
            raise ExecutorError("ACTION_LEASE_EXPIRED", "The action lease has expired.")
        with self.lock:
            self.x_grant = {
                "action_id": action_id,
                "claim_token": claim_token,
                "expires": float(lease_expires_epoch),
                "action_type": action_type,
            }
        return {
            "authorized": True,
            "action_id": action_id,
            "action_type": action_type,
            "single_use": True,
        }

    def _consume_x_grant(self, allowed: frozenset[str]) -> dict[str, Any]:
        grant = self.x_grant
        self.x_grant = None
        if not isinstance(grant, dict):
            raise ExecutorError(
                "X_WRITE_CONFIRMATION_REQUIRED",
                "Generic clicks on X are disabled; claim a confirmed action first.",
            )
        if float(grant.get("expires", 0)) <= time.time():
            raise ExecutorError("ACTION_LEASE_EXPIRED", "The action lease has expired.")
        if grant.get("action_type") not in allowed:
            raise ExecutorError(
                "X_ACTION_SCOPE_MISMATCH",
                "This claimed action does not permit that browser step.",
                details={"action_type": grant.get("action_type")},
            )
        return grant

    def _assert_visual_snapshot(self, observation_id: str) -> None:
        if self.visual_observation_id != observation_id or not self.visual_digest:
            raise ExecutorError("SCREENSHOT_REQUIRED", "Capture this observation before a coordinate click.")
        current = self.page.screenshot(type="png", full_page=False)
        if hashlib.sha256(current).hexdigest() != self.visual_digest:
            self.observation_id = None
            self.visual_observation_id = None
            raise ExecutorError("OBSERVATION_STALE", "Visible pixels changed; take a new observation and screenshot.")

    def _dispatch_click(
        self, selector: str | None, x_ratio: float | None, y_ratio: float | None
    ) -> dict[str, Any]:
        if selector:
            self.page.locator(selector).click(timeout=5000)
        else:
            width = int(self.page.evaluate("innerWidth"))
            height = int(self.page.evaluate("innerHeight"))
            self.page.mouse.click(round(x_ratio * width), round(y_ratio * height))
        self.page.wait_for_timeout(300)
        return self._observe_impl()

    def close(self) -> None:
        if self.worker and self.worker.is_alive() and threading.get_ident() != self.worker_ident:
            try:
                # Cleanup must still be attempted after a timed-out task.
                self._run_on_worker(self._close_impl, timeout=15, allow_stuck=True)
            except ExecutorError:
                # The close task stays queued. It runs as soon as the stuck
                # operation returns, so the browser does not leak.
                pass
            finally:
                self.tasks.put(None)
                self.worker.join(timeout=3)
                if self.worker is not None and not self.worker.is_alive():
                    self.worker = None
                    self.worker_ident = None
                    self.timed_out = False
                else:
                    # The stuck task is still holding the worker. Do not accept
                    # new work on this instance; its owner should discard it.
                    self.timed_out = True
            return
        self._close_impl()

    def _close_impl(self) -> None:
        with self.lock:
            if self.context:
                try:
                    self.context.close()
                except Exception:
                    pass
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    pass
            if self.playwright:
                try:
                    self.playwright.stop()
                except Exception:
                    pass
            self.browser = None
            self.page = None
            self.context = None
            self.playwright = None
            self.observation_id = None
            self.visual_observation_id = None
            self.visual_digest = None
            self.x_grant = None


BROWSER = Browser()


TOOLS = [
    {"name": "browser_type", "description": "Type bounded text into the focused field, requiring a current observation. On X this consumes a confirmed action claim.", "inputSchema": {"type": "object", "required": ["observation_id", "fingerprint_sha256", "text"], "properties": {"observation_id": {"type": "string", "minLength": 1, "maxLength": 64}, "fingerprint_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "text": {"type": "string", "minLength": 1, "maxLength": 280}}, "additionalProperties": False}, "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "browser_scroll", "description": "Scroll the visible document by bounded vertical pixels, requiring a current observation. Returns a fresh observation; does not click or type.", "inputSchema": {"type": "object", "required": ["observation_id", "fingerprint_sha256", "delta_y"], "properties": {"observation_id": {"type": "string", "minLength": 1, "maxLength": 64}, "fingerprint_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "delta_y": {"type": "integer", "minimum": -2000, "maximum": 2000}}, "additionalProperties": False}, "annotations": {"readOnlyHint": True, "idempotentHint": False}},
    {"name": "browser_open", "description": "Open an allowlisted URL in the dedicated browser profile.", "inputSchema": {"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}, "additionalProperties": False}, "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "browser_observe", "description": "Read visible title, URL, text, and safe viewport metadata.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": {"readOnlyHint": True, "idempotentHint": True}},
    {"name": "browser_screenshot", "description": "Capture the current visible page after an observation check. Data is omitted unless include_data is true.", "inputSchema": {"type": "object", "required": ["observation_id", "fingerprint_sha256"], "properties": {"observation_id": {"type": "string"}, "fingerprint_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "include_data": {"type": "boolean", "default": False}}, "additionalProperties": False}, "annotations": {"readOnlyHint": True, "idempotentHint": True}},
    {"name": "browser_click", "description": "Perform one selector or coordinate click only when the observation fingerprint is unchanged.", "inputSchema": {"type": "object", "required": ["observation_id", "fingerprint_sha256"], "properties": {"observation_id": {"type": "string"}, "fingerprint_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "selector": {"type": "string", "maxLength": 240}, "x_ratio": {"type": "number", "minimum": 0, "maximum": 1}, "y_ratio": {"type": "number", "minimum": 0, "maximum": 1}}, "additionalProperties": False}, "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "browser_close", "description": "Close the dedicated browser process.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": {"readOnlyHint": False, "destructiveHint": False}},
]


def handle(request: dict[str, Any]) -> dict[str, Any]:
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "x-browser-executor", "version": "0.1.0"},
            "instructions": "Use only the dedicated visible browser profile. Do not read cookies, tokens, passwords, or private storage.",
        }
    elif method == "notifications/initialized":
        return {}
    elif method == "ping":
        result = {}
    elif method == "server/discover":
        if request_id is None:
            return {}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found: server/discover"},
        }
    elif method == "resources/list":
        result = {"resources": []}
    elif method == "prompts/list":
        result = {"prompts": []}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = request.get("params", {}).get("name")
        args = request.get("params", {}).get("arguments", {})
        _audit({"event": "tool_call", "tool": name})
        try:
            if name == "browser_open":
                data = BROWSER.open(str(args.get("url", "")))
            elif name == "browser_observe":
                data = BROWSER.observe()
            elif name == "browser_scroll":
                data = BROWSER.scroll(
                    observation_id=str(args.get("observation_id", "")),
                    fingerprint_sha256=str(args.get("fingerprint_sha256", "")),
                    delta_y=args.get("delta_y"),
                )
            elif name == "browser_type":
                data = BROWSER.type_text(
                    observation_id=str(args.get("observation_id", "")),
                    fingerprint_sha256=str(args.get("fingerprint_sha256", "")),
                    text=args.get("text"),
                )
            elif name == "browser_screenshot":
                data = BROWSER.screenshot(
                    str(args.get("observation_id", "")),
                    str(args.get("fingerprint_sha256", "")),
                    bool(args.get("include_data", False)),
                )
            elif name == "browser_click":
                data = BROWSER.click(
                    observation_id=str(args.get("observation_id", "")),
                    fingerprint_sha256=str(args.get("fingerprint_sha256", "")),
                    selector=args.get("selector"),
                    x_ratio=args.get("x_ratio"),
                    y_ratio=args.get("y_ratio"),
                )
            elif name == "browser_close":
                BROWSER.close()
                data = {"closed": True}
            else:
                raise ExecutorError("UNKNOWN_TOOL", "Unknown browser executor tool.")
            result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "structuredContent": data}
            _audit({"event": "tool_result", "tool": name, "ok": True})
        except ExecutorError as exc:
            _audit({"event": "tool_result", "tool": name, "ok": False, "code": exc.code})
            result = {"isError": True, "content": [{"type": "text", "text": json.dumps({"code": exc.code, "message": exc.message})}]}
    else:
        result = {}
    if request_id is None:
        return {}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/mcp":
            self.send_error(404)
            return
        payload = b": browser-executor-ready\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        print(f"POST {self.path}", flush=True)
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        print(f"METHOD {request.get('method')}", flush=True)
        response = handle(request)
        payload = _json(response)
        notification = request.get("id") is None
        self.send_response(202 if notification else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Mcp-Session-Id", "browser-executor-session")
        self.end_headers()
        if not notification:
            self.wfile.write(payload)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> None:
    # Playwright's sync API is thread-affine. A single request thread keeps
    # browser operations on the thread that created the Playwright runtime.
    server = HTTPServer((HOST, PORT), Handler)
    try:
        print(f"x-browser-executor listening on http://{HOST}:{PORT}/mcp", flush=True)
        server.serve_forever()
    finally:
        BROWSER.close()
        server.server_close()


if __name__ == "__main__":
    main()
