"""Open the owner profile in a visible browser so a human can sign in to X.

The agent must never type a password, email code, or OAuth consent. This
script only opens the profile and leaves the credential entry to the owner.
It refuses to run when DISPLAY-less headless mode is requested.

The process stays alive while the window is open. Playwright's driver is a
child of this process, so exiting early would close the window before the
owner can finish signing in.

Usage:
    $env:X_STARTER_OWNER_PROFILE = "C:\\Users\\me\\AppData\\Local\\x-starter\\owner-profiles\\owner-x"
    python -X utf8 owner_login_open.py

Then sign in, confirm `owner_login_check.py` reports signed_in, and close the
window. Cookies stay in the profile directory; nothing is printed.

Optional:
    X_STARTER_OWNER_LOGIN_WAIT     seconds to keep the window open (default 1800)
    X_STARTER_OWNER_LOGIN_SENTINEL file whose creation also ends the wait
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from browser_executor import Browser, ExecutorError  # noqa: E402


def _page_closed(browser: Browser) -> bool:
    """Report whether the owner window is gone, without touching page data."""

    def check() -> bool:
        page = browser.page
        if page is None:
            return True
        try:
            return bool(page.is_closed())
        except Exception:
            return True

    try:
        return bool(browser._run_on_worker(check, timeout=10))
    except ExecutorError:
        return True


def wait_for_owner(browser: Browser, *, wait_seconds: float, sentinel: Path | None = None) -> str:
    """Block until the owner closes the window, creates the sentinel, or time runs out."""

    deadline = time.monotonic() + max(wait_seconds, 0)
    while time.monotonic() < deadline:
        if sentinel is not None and sentinel.exists():
            return "sentinel"
        if _page_closed(browser):
            return "window_closed"
        time.sleep(1.5)
    return "timeout"


def main() -> int:
    profile = os.environ.get("X_STARTER_OWNER_PROFILE", "").strip()
    if not profile:
        print(json.dumps({
            "ok": False,
            "error": "OWNER_PROFILE_NOT_SET",
            "detail": "Set X_STARTER_OWNER_PROFILE first.",
        }, ensure_ascii=True))
        return 2
    directory = Path(profile).expanduser().resolve()
    if not directory.is_dir():
        print(json.dumps({
            "ok": False,
            "error": "OWNER_PROFILE_MISSING",
            "detail": "Create the directory on fast local storage first.",
        }, ensure_ascii=True))
        return 2
    if os.environ.get("X_BROWSER_EXECUTOR_HEADLESS") == "1":
        print(json.dumps({
            "ok": False,
            "error": "HEADLESS_FORBIDDEN",
            "detail": "A human cannot sign in to a headless window.",
        }, ensure_ascii=True))
        return 2

    os.environ["X_BROWSER_EXECUTOR_ALLOWED_HOSTS"] = (
        "x.com,www.x.com,twitter.com,www.twitter.com"
    )
    browser = Browser(profile=directory)
    try:
        observation = browser.open("https://x.com/home")
    except ExecutorError as exc:
        print(json.dumps({
            "ok": False, "error": exc.code,
            "detail": exc.message.splitlines()[0][:300],
        }, ensure_ascii=True))
        browser.close()
        return 1
    print(json.dumps({
        "ok": True,
        "profile_is_local_fast_storage": str(directory).lower().startswith(
            os.environ.get("LOCALAPPDATA", "").lower()
        ),
        "url": observation["url"],
        "window": "open",
        "next": (
            "Sign in manually in the visible window. The agent will not type "
            "credentials or codes. Then run owner_login_check.py and close the "
            "window. This process exits when the window closes."
        ),
    }, ensure_ascii=True), flush=True)

    sentinel_text = os.environ.get("X_STARTER_OWNER_LOGIN_SENTINEL", "").strip()
    sentinel = Path(sentinel_text).expanduser() if sentinel_text else None
    wait_seconds = float(os.environ.get("X_STARTER_OWNER_LOGIN_WAIT", "1800"))
    try:
        reason = wait_for_owner(browser, wait_seconds=wait_seconds, sentinel=sentinel)
    except KeyboardInterrupt:
        reason = "interrupted"
    print(json.dumps({"ok": True, "closed_by": reason}, ensure_ascii=True), flush=True)
    browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
