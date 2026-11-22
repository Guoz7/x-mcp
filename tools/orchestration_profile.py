"""Load the measured orchestration profile and answer the driver's questions.

The profile is a record of what a real account did, so the loader validates it
instead of trusting it: an out-of-range value would silently produce a wave that
X refuses, and the driver would then look broken for the wrong reason.

Usage:
    python -X utf8 tools/orchestration_profile.py --show
    python -X utf8 tools/orchestration_profile.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "docs" / "orchestration-profile.json"

# Bounds come from the measured behaviour, not from preference.
LIMITS = {
    ("follow", "per_window"): (1, 15),        # 15 was the observed endpoint cap
    ("follow", "window_seconds"): (60, 3600),
    ("follow", "gap_seconds"): (0, 120),
    ("repost", "per_wave"): (1, 50),
    ("repost", "gap_seconds"): (0, 600),
    ("repost", "max_barren_waves"): (1, 20),
    ("runtime", "nav_timeout_ms"): (1000, 60000),
    ("pool", "replenish_below"): (1, 5000),
    ("pool", "per_harvest"): (10, 5000),
}

REQUIRED = (
    ("follow", "per_window"),
    ("follow", "window_seconds"),
    ("repost", "per_wave"),
    ("runtime", "nav_timeout_ms"),
)


class ProfileError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def load(path: Path | None = None) -> dict[str, Any]:
    target = path or DEFAULT
    if not target.is_file():
        raise ProfileError("PROFILE_MISSING", "No orchestration profile at %s" % target)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ProfileError("PROFILE_INVALID_JSON", str(exc)) from None
    if not isinstance(data, dict):
        raise ProfileError("PROFILE_INVALID", "The profile must be a JSON object.")
    return data


def validate(profile: dict[str, Any]) -> list[str]:
    """Return a list of problems; an empty list means the profile is usable."""
    problems: list[str] = []

    for section, key in REQUIRED:
        block = profile.get(section)
        if not isinstance(block, dict) or key not in block:
            problems.append("missing %s.%s" % (section, key))

    for (section, key), (low, high) in LIMITS.items():
        block = profile.get(section)
        if not isinstance(block, dict) or key not in block:
            continue
        value = block[key]
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append("%s.%s must be an integer" % (section, key))
            continue
        if not low <= value <= high:
            problems.append("%s.%s must be from %d to %d (got %d)" % (
                section, key, low, high, value))

    states = profile.get("success_states")
    if not isinstance(states, list) or not states:
        problems.append("success_states must be a non-empty list")
    elif not all(isinstance(s, str) for s in states):
        problems.append("success_states must contain strings")

    for section in ("follow", "repost", "like"):
        block = profile.get(section)
        if not isinstance(block, dict):
            problems.append("missing %s section" % section)
            continue
        if "enabled" in block and not isinstance(block["enabled"], bool):
            problems.append("%s.enabled must be a boolean" % section)

    return problems


def summary(profile: dict[str, Any]) -> dict[str, Any]:
    """The values a driver needs, with the derived numbers already computed."""
    follow = profile.get("follow") or {}
    repost = profile.get("repost") or {}
    per_window = follow.get("per_window")
    window = follow.get("window_seconds")
    waves_for_200 = None
    minutes_for_200 = None
    if isinstance(per_window, int) and isinstance(window, int) and per_window > 0:
        waves_for_200 = -(-200 // per_window)
        minutes_for_200 = round(waves_for_200 * window / 60, 1)
    return {
        "account": profile.get("account"),
        "follow_enabled": bool(follow.get("enabled")),
        "follow_per_window": per_window,
        "follow_window_seconds": window,
        "follow_waves_for_200": waves_for_200,
        "follow_minutes_for_200": minutes_for_200,
        "repost_enabled": bool(repost.get("enabled")),
        "repost_per_wave": repost.get("per_wave"),
        "repost_gap_seconds": repost.get("gap_seconds"),
        "like_enabled": bool((profile.get("like") or {}).get("enabled")),
        "nav_timeout_ms": (profile.get("runtime") or {}).get("nav_timeout_ms"),
        "success_states": profile.get("success_states"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=None)
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    try:
        profile = load(args.profile)
    except ProfileError as exc:
        print(json.dumps({"ok": False, "error": exc.code, "message": exc.message}))
        return 2

    problems = validate(profile)
    if args.check:
        payload = {"ok": not problems, "problems": problems}
    else:
        payload = {"ok": not problems, "problems": problems, "summary": summary(profile)}
        if args.show:
            payload["profile"] = profile
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
