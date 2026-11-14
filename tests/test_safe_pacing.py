"""The safe profile must reject the pace that got the last account suspended.

The previous run used a median of 7 seconds between reposts, pushed 1,914
writes in 45 hours, and was suspended. These tests pin the rules that would
have stopped it, so a future plan cannot quietly reintroduce that pacing.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from safe_pacing import (  # noqa: E402
    Budget,
    DEFAULT_PROFILE,
    PacingError,
    check_plan,
    load_profile,
)


def _profile() -> dict:
    return {
        "circuit_breaker": {
            "stop_on_any_refusal": True,
            "refusal_codes": ["283", "161", "141", "37"],
            "stop_on_uncertain_streak": 2,
        },
        "follow": {"enabled": True, "per_day": 25, "min_gap_seconds": 90},
        "repost": {"enabled": True, "per_day": 40, "min_gap_seconds": 120},
        "like": {"enabled": True, "per_day": 60, "min_gap_seconds": 60},
    }


class ShippedProfileTest(unittest.TestCase):
    def test_the_shipped_profile_loads(self):
        profile = load_profile(DEFAULT_PROFILE)
        self.assertEqual(profile["circuit_breaker"]["stop_on_any_refusal"], True)

    def test_every_enabled_kind_has_a_floor_of_a_minute(self):
        profile = load_profile(DEFAULT_PROFILE)
        for kind in ("follow", "repost", "like"):
            block = profile[kind]
            if not block.get("enabled", True):
                continue
            self.assertGreaterEqual(block["min_gap_seconds"], 60, kind)

    def test_the_daily_ceiling_is_far_below_the_measured_endpoint_cap(self):
        profile = load_profile(DEFAULT_PROFILE)
        # The follow endpoint measured 15 per 900 s; the profile must not
        # approach that as a daily target.
        self.assertLessEqual(profile["follow"]["per_day"], 30)

    def test_the_last_run_pace_would_be_rejected(self):
        """1914 actions in 45 hours is the plan that must not pass."""
        profile = load_profile(DEFAULT_PROFILE)
        per_day = round(1914 / 45.6 * 24)
        problems = check_plan(profile, {"repost": per_day})
        self.assertTrue(problems, "a 1000/day repost plan must be refused")


class CheckPlanTest(unittest.TestCase):
    def test_a_plan_inside_every_cap_passes(self):
        self.assertEqual(check_plan(_profile(), {"follow": 20, "like": 40, "repost": 30}), [])

    def test_exceeding_a_daily_cap_is_reported(self):
        problems = check_plan(_profile(), {"follow": 200})
        self.assertTrue(any("follow per_day" in p for p in problems))

    def test_the_exact_cap_is_allowed(self):
        problems = check_plan(_profile(), {"follow": 25})
        self.assertEqual([p for p in problems if "per_day" in p], [])

    def test_a_negative_count_is_reported(self):
        problems = check_plan(_profile(), {"like": -1})
        self.assertTrue(any("negative" in p for p in problems))

    def test_a_disabled_kind_cannot_be_planned(self):
        profile = _profile()
        profile["like"]["enabled"] = False
        problems = check_plan(profile, {"like": 5})
        self.assertTrue(any("disabled" in p for p in problems))

    def test_a_sub_minute_gap_floor_is_rejected(self):
        profile = _profile()
        profile["repost"]["min_gap_seconds"] = 7
        problems = check_plan(profile, {})
        self.assertTrue(any("below the 60 s floor" in p for p in problems))

    def test_a_missing_section_is_reported(self):
        profile = _profile()
        del profile["like"]
        problems = check_plan(profile, {})
        self.assertTrue(any("missing like section" in p for p in problems))


class BudgetTest(unittest.TestCase):
    def test_the_first_write_is_admitted(self):
        budget = Budget(_profile())
        self.assertTrue(budget.admit("repost", now=1000.0))

    def test_a_second_write_inside_the_gap_is_refused(self):
        budget = Budget(_profile())
        budget.record("repost", now=1000.0)
        self.assertFalse(budget.admit("repost", now=1010.0))

    def test_a_write_after_the_gap_is_admitted(self):
        budget = Budget(_profile())
        budget.record("repost", now=1000.0)
        self.assertTrue(budget.admit("repost", now=1130.0))

    def test_the_daily_cap_stops_further_writes(self):
        budget = Budget(_profile())
        for i in range(25):
            budget.record("follow", now=1000.0 + i * 120)
        self.assertFalse(budget.admit("follow", now=1000.0 + 25 * 120))

    def test_a_refusal_closes_the_day(self):
        budget = Budget(_profile())
        budget.observe_refusal(283)
        self.assertEqual(budget.stopped(), "refusal:283")
        self.assertFalse(budget.admit("repost", now=1000.0))

    def test_the_suspension_code_closes_the_day(self):
        budget = Budget(_profile())
        budget.observe_refusal(141)
        self.assertFalse(budget.admit("like", now=1000.0))

    def test_an_uncertain_streak_closes_the_day(self):
        budget = Budget(_profile())
        budget.observe_uncertainty(1)
        self.assertIsNone(budget.stopped())
        budget.observe_uncertainty(2)
        self.assertEqual(budget.stopped(), "uncertain_streak:2")

    def test_a_new_day_reopens_the_budget(self):
        budget = Budget(_profile())
        budget.observe_refusal(37)
        budget.reset_day()
        self.assertTrue(budget.admit("repost", now=1000.0))

    def test_usage_is_reported_per_kind(self):
        budget = Budget(_profile())
        budget.record("like", now=1000.0)
        budget.record("like", now=1200.0)
        budget.record("follow", now=1000.0)
        self.assertEqual(budget.usage(), {"like": 2, "follow": 1})

    def test_a_disabled_kind_is_never_admitted(self):
        profile = _profile()
        profile["repost"]["enabled"] = False
        budget = Budget(profile)
        self.assertFalse(budget.admit("repost", now=1000.0))


class LoadTest(unittest.TestCase):
    def test_a_missing_profile_raises_a_typed_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PacingError) as caught:
                load_profile(Path(tmp) / "absent.json")
            self.assertEqual(caught.exception.code, "PROFILE_MISSING")

    def test_invalid_json_raises_a_typed_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{nope", encoding="utf-8")
            with self.assertRaises(PacingError) as caught:
                load_profile(path)
            self.assertEqual(caught.exception.code, "PROFILE_INVALID_JSON")


if __name__ == "__main__":
    unittest.main()
