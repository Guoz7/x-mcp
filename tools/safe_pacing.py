"""Enforce the post-suspension pacing profile.

The previous run was suspended after 1,914 writes in 45 hours, with a median of
7 seconds between reposts. Advice was not enough; these checks make an
over-paced plan fail before it runs.

`check_plan` answers whether a proposed schedule stays inside the profile.
`Budget` is a runtime gate a driver must ask before every write.

Usage:
    python -X utf8 tools/safe_pacing.py --profile docs/orchestration-profile-safe.json --check
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = ROOT / "docs" / "orchestration-profile-safe.json"

KINDS = ("follow", "repost", "like")


class PacingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def load_profile(path: Path | None = None) -> dict[str, Any]:
    target = path or DEFAULT_PROFILE
    if not target.is_file():
        raise PacingError("PROFILE_MISSING", "No profile at %s" % target)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise PacingError("PROFILE_INVALID_JSON", str(exc)) from None
    if not isinstance(data, dict):
        raise PacingError("PROFILE_INVALID", "The profile must be a JSON object.")
    return data


def check_plan(profile: dict[str, Any], plan: dict[str, int]) -> list[str]:
    """Return a list of violations; empty means the plan is inside the profile."""
    problems: list[str] = []
    for kind in KINDS:
        block = profile.get(kind)
        if not isinstance(block, dict):
            problems.append("missing %s section" % kind)
            continue
        wanted = plan.get(kind, 0)
        if wanted < 0:
            problems.append("%s count must not be negative" % kind)
            continue
        if not block.get("enabled", True) and wanted:
            problems.append("%s is disabled but the plan asks for %d" % (kind, wanted))
            continue
        cap = block.get("per_day")
        if isinstance(cap, int) and wanted > cap:
            problems.append("%s per_day %d exceeds the safe cap %d" % (kind, wanted, cap))

        gap = block.get("min_gap_seconds")
        if isinstance(gap, int) and gap < 60:
            problems.append("%s min_gap_seconds %d is below the 60 s floor" % (kind, gap))
    return problems


@dataclass
class Budget:
    """A runtime gate: ask before every write, and record what was allowed."""

    profile: dict[str, Any]
    _used: dict[str, int] = field(default_factory=dict)
    _last: dict[str, float] = field(default_factory=dict)
    _stopped: str | None = None
    _stop_reason: str | None = None

    def _block(self, kind: str) -> dict[str, Any]:
        block = self.profile.get(kind)
        if not isinstance(block, dict):
            raise PacingError("PROFILE_MISSING_SECTION", "No %s section." % kind)
        return block

    def stopped(self) -> str | None:
        return self._stop_reason

    def stop(self, reason: str) -> None:
        """Close the day. Nothing else is admitted until reset."""
        self._stopped = reason
        self._stop_reason = reason

    def reset_day(self) -> None:
        self._used.clear()
        self._stopped = None
        self._stop_reason = None

    def admit(self, kind: str, now: float) -> bool:
        """True when one more write of this kind is allowed right now."""
        if self._stopped:
            return False
        block = self._block(kind)
        if not block.get("enabled", True):
            return False

        cap = block.get("per_day")
        if isinstance(cap, int) and self._used.get(kind, 0) >= cap:
            return False

        gap = block.get("min_gap_seconds")
        last = self._last.get(kind)
        if isinstance(gap, int) and last is not None and now - last < gap:
            return False
        return True

    def record(self, kind: str, now: float) -> None:
        self._used[kind] = self._used.get(kind, 0) + 1
        self._last[kind] = now

    def observe_refusal(self, code: Any) -> None:
        """A refusal closes the day when the profile says so."""
        breaker = self.profile.get("circuit_breaker") or {}
        if not breaker.get("stop_on_any_refusal"):
            return
        listed = {str(c) for c in (breaker.get("refusal_codes") or [])}
        if code is None or str(code) in listed or not listed:
            self.stop("refusal:%s" % code)

    def observe_uncertainty(self, streak: int) -> None:
        breaker = self.profile.get("circuit_breaker") or {}
        limit = breaker.get("stop_on_uncertain_streak")
        if isinstance(limit, int) and streak >= limit:
            self.stop("uncertain_streak:%d" % streak)

    def usage(self) -> dict[str, int]:
        return dict(self._used)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=None)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--plan", default=None,
                        help='JSON like {"follow": 25, "like": 60, "repost": 40}')
    args = parser.parse_args()

    try:
        profile = load_profile(args.profile)
    except PacingError as exc:
        print(json.dumps({"ok": False, "error": exc.code, "message": exc.message}))
        return 2

    plan = {}
    if args.plan:
        try:
            plan = json.loads(args.plan)
        except ValueError:
            print(json.dumps({"ok": False, "error": "PLAN_INVALID_JSON"}))
            return 2

    problems = check_plan(profile, plan) if plan else []
    if args.check and not plan:
        for kind in KINDS:
            block = profile.get(kind) or {}
            if not block.get("enabled", True):
                continue
            gap = block.get("min_gap_seconds")
            if not isinstance(gap, int) or gap < 60:
                problems.append("%s min_gap_seconds missing or below 60 s" % kind)
            if not isinstance(block.get("per_day"), int):
                problems.append("%s per_day missing" % kind)
        breaker = profile.get("circuit_breaker") or {}
        if not breaker.get("stop_on_any_refusal"):
            problems.append("circuit_breaker.stop_on_any_refusal must be true")

    print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=False, indent=1))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
