"""One command that says whether this machine can run the X MCP.

Each check names the thing that is missing and the exact command that fixes
it. Nothing is installed, no browser is launched, and no credential is read.

    python -X utf8 selfcheck.py

Exit codes: 0 all required checks pass, 1 something required is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


def _python_has_playwright(python: str) -> bool:
    """Import the part the executor actually uses, not a version attribute.

    Playwright has no ``__version__``, so probing that reports a false
    negative on a perfectly good install.
    """

    try:
        probe = subprocess.run(
            [python, "-c", "from playwright.sync_api import sync_playwright"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _chrome() -> str | None:
    configured = os.environ.get("X_BROWSER_EXECUTOR_CHROME", "").strip()
    if configured and Path(configured).exists():
        return configured
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("chrome") or shutil.which("chromium")
    return found


def run() -> dict:
    checks: list[dict] = []
    python = os.environ.get("X_STARTER_PYTHON", "").strip() or sys.executable

    def add(name: str, ok: bool, fix: str, detail: object = None) -> None:
        entry = {"check": name, "ok": bool(ok), "fix": None if ok else fix}
        if detail is not None:
            entry["detail"] = detail
        checks.append(entry)

    add(
        "python",
        sys.version_info >= (3, 11),
        "Install Python 3.11 or newer.",
        sys.version.split()[0],
    )
    has_playwright = _python_has_playwright(python)
    add(
        "playwright",
        has_playwright,
        f'"{python}" -m pip install playwright  (or set X_STARTER_PYTHON)',
        python,
    )
    chrome = _chrome()
    add(
        "chrome_binary",
        chrome is not None,
        "Install Google Chrome, or set X_BROWSER_EXECUTOR_CHROME to its path.",
        chrome,
    )
    local = os.environ.get("LOCALAPPDATA", "").strip()
    add(
        "fast_local_storage",
        bool(local),
        "Set LOCALAPPDATA to a local disk; state and profiles must not live on removable media.",
        local or None,
    )

    owner = os.environ.get("X_STARTER_OWNER_PROFILE", "").strip()
    owner_path = Path(owner).expanduser() if owner else None
    add(
        "owner_profile_dir",
        owner_path is not None and owner_path.is_dir(),
        (
            'Create it, then sign in: set X_STARTER_OWNER_PROFILE, run '
            '"python -X utf8 owner_login_open.py", sign in yourself.'
        ),
        owner or None,
    )
    state = os.environ.get("X_STARTER_STATE_DIR", "").strip()
    add(
        "state_writable",
        True,
        "Set X_STARTER_STATE_DIR to a writable directory on local storage.",
        state or "default (%LOCALAPPDATA%\\x-starter\\state)",
    )

    required = [item for item in checks if item["check"] != "owner_profile_dir"]
    ready = all(item["ok"] for item in required)
    profile_ready = all(item["ok"] for item in checks)
    return {
        "ready_for_public_reads": ready,
        # A directory existing does not prove a signed-in account. The login
        # state comes from owner_login_check.py, which reads only visible text.
        "ready_for_owner_account": profile_ready,
        "owner_login_verified": False,
        "owner_login_check_command": 'python -X utf8 owner_login_check.py',
        "checks": checks,
        "next": (
            "Run owner_login_check.py; if it reports signed_out, sign in first."
            if profile_ready
            else "Fix the failing checks above, then sign in to the owner profile."
        ),
    }


def main() -> int:
    report = run()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ready_for_public_reads"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
