"""Open the owner profile in a visible browser so a human can sign in to X.

The agent must never type a password, email code, or OAuth consent. This
script only opens the profile and leaves the credential entry to the owner.
It refuses to run when DISPLAY-less headless mode is requested.

The default launcher uses ordinary Chrome without an automation connection.
After the owner closes Chrome, the MCP executor can reuse the same profile.
Set X_STARTER_OWNER_LOGIN_BACKEND=playwright only for launcher diagnostics.

Usage:
    $env:X_STARTER_OWNER_PROFILE = "C:\\Users\\me\\AppData\\Local\\x-starter\\owner-profiles\\owner-x"
    python -X utf8 owner_login_open.py

Then sign in and close the window before running owner_login_check.py.
The owner profile is not exported or inspected by this launcher.

Optional:
    X_STARTER_OWNER_LOGIN_WAIT     seconds to keep the window open (default 1800)
    X_STARTER_OWNER_LOGIN_SENTINEL file whose creation also ends the wait
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from browser_executor import Browser, ExecutorError, _user_agent  # noqa: E402
from browser_paths import find_chrome  # noqa: E402


def open_native(directory: Path, *, wait_seconds: float) -> int:
    """Leave authentication to the human, without attaching an automation client."""

    executable = find_chrome()
    if not executable:
        print(json.dumps({"ok": False, "error": "BROWSER_BINARY_MISSING"}))
        return 2
    command = [
        executable, "--user-data-dir=" + str(directory),
        "--no-first-run", "--no-default-browser-check", "--disable-sync",
        "--user-agent=" + _user_agent(), "--new-window", "https://x.com/home",
    ]
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        print(json.dumps({"ok": False, "error": "OWNER_BROWSER_START_FAILED"}))
        return 1
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    else:
        # Chrome may hand the URL to an already-running profile and exit.
        print(json.dumps({
            "ok": False, "error": "OWNER_BROWSER_EARLY_EXIT",
            "detail": "Close this dedicated profile's existing windows, then retry.",
        }))
        return 3
    print(json.dumps({
        "ok": True, "backend": "native", "window": "launched",
        "authentication_verified": False,
        "next": "Sign in manually, then close this dedicated browser before MCP use.",
    }), flush=True)
    try:
        code = process.wait(timeout=max(wait_seconds, 0.01))
    except subprocess.TimeoutExpired:
        print(json.dumps({
            "ok": False, "error": "OWNER_LOGIN_WAIT_EXPIRED",
            "window_left_open": True,
            "detail": "No login state was inspected. Close the dedicated browser before MCP use.",
        }), flush=True)
        return 3
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "OWNER_LOGIN_WAIT_INTERRUPTED", "window_left_open": True}))
        return 3
    print(json.dumps({
        "ok": code == 0, "closed_by": "browser_exit",
        "authentication_verified": False,
    }), flush=True)
    return 0 if code == 0 else 1


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

    backend = os.environ.get("X_STARTER_OWNER_LOGIN_BACKEND", "native")
    if backend == "native":
        return open_native(
            directory,
            wait_seconds=float(os.environ.get("X_STARTER_OWNER_LOGIN_WAIT", "1800")),
        )
    if backend != "playwright":
        print(json.dumps({"ok": False, "error": "OWNER_LOGIN_BACKEND_INVALID"}))
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
            "credentials or codes. Close the window before running "
            "owner_login_check.py. This process exits when the window closes."
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
