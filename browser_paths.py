"""Browser lookup shared by startup and diagnostics."""

import os
from pathlib import Path
import shutil


def find_chrome() -> str | None:
    configured = os.environ.get("X_BROWSER_EXECUTOR_CHROME", "").strip()
    if configured:
        return configured if Path(configured).is_file() else None
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("chrome") or shutil.which("chromium")
