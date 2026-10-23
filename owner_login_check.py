"""Report whether an owner Chromium profile is signed in to X.

This is a read-only diagnostic. It opens the owner profile, reads only the
visible page, and classifies the result. It never reads cookies, storage,
passwords, or tokens, and it never clicks, types, or posts.

Usage:
    $env:X_STARTER_OWNER_PROFILE = "D:\\path\\to\\owner-profile"
    python -X utf8 owner_login_check.py

Unset X_BROWSER_EXECUTOR_HEADLESS to watch the page, or set it to 1 to run
headless. Node/Playwright are required, as for the rest of the executor.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from browser_executor import Browser, ExecutorError  # noqa: E402


SIGNED_IN_MARKERS = ("/home", "/notifications", "/messages", "/explore")
SIGNED_OUT_URL_MARKERS = (
    "/i/jf/onboarding",
    "redirect_after_login",
    "/i/flow/login",
    "/login",
    "/i/flow/signup",
)
SIGNED_OUT_MARKERS = (
    "continue with phone",
    "sign in",
    "log in",
    "使用手机继续",
    "登录",
    "注册",
)


def classify(url: str, text: str) -> str:
    lowered = text.lower()
    # The login wall can redirect to an onboarding URL with no visible text.
    if any(marker in url for marker in SIGNED_OUT_URL_MARKERS):
        return "signed_out"
    if any(marker in url for marker in SIGNED_IN_MARKERS) and "redirect_after_login" not in url:
        return "signed_in"
    if any(marker in lowered for marker in SIGNED_OUT_MARKERS):
        return "signed_out"
    return "unknown"


def main() -> int:
    profile = os.environ.get("X_STARTER_OWNER_PROFILE", "").strip()
    if not profile:
        print(json.dumps({
            "ok": False,
            "error": "OWNER_PROFILE_NOT_SET",
            "detail": "Set X_STARTER_OWNER_PROFILE to the owner profile directory.",
        }, ensure_ascii=True))
        return 2
    if not Path(profile).is_dir():
        print(json.dumps({
            "ok": False,
            "error": "OWNER_PROFILE_MISSING",
            "detail": "The profile directory does not exist yet. Sign in once to create it.",
        }, ensure_ascii=True))
        return 2

    os.environ["X_BROWSER_EXECUTOR_ALLOWED_HOSTS"] = (
        "x.com,www.x.com,twitter.com,www.twitter.com"
    )
    browser = Browser(profile=Path(profile))
    try:
        observation = browser.open("https://x.com/home")
    except ExecutorError as exc:
        print(json.dumps({
            "ok": False,
            "error": exc.code,
            "detail": exc.message.splitlines()[0][:300],
        }, ensure_ascii=True))
        browser.close()
        return 1
    try:
        state = classify(observation["url"], observation["text"])
        print(json.dumps({
            "ok": True,
            "state": state,
            "url": observation["url"],
            "title": observation["title"][:120],
            "visible_characters": len(observation["text"]),
            "viewport": [observation["width"], observation["height"]],
            "note": (
                "Read-only classification from visible page text. "
                "No cookies, storage, or credentials were accessed."
            ),
        }, ensure_ascii=True))
    finally:
        browser.close()
    return 0 if state in {"signed_in", "signed_out"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
