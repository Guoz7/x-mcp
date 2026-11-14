"""The orchestration profile must reject values X would refuse.

The numbers are a record of measured behaviour, so a value outside the measured
range would produce a wave that fails for a reason the driver cannot explain:
the profile would look fine while the account kept getting 429s.
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

from orchestration_profile import (  # noqa: E402
    DEFAULT,
    ProfileError,
    load,
    summary,
    validate,
)


def _base() -> dict:
    return {
        "account": "fixture",
        "runtime": {"nav_timeout_ms": 45000},
        "follow": {
            "per_window": 14,
            "window_seconds": 900,
            "gap_seconds": 4,
            "enabled": True,
        },
        "repost": {"per_wave": 4, "gap_seconds": 20, "enabled": True},
        "like": {"enabled": False},
        "pool": {"replenish_below": 60, "per_harvest": 200},
        "success_states": ["following", "liked", "reposted", "replied"],
    }


class ProfileValidationTest(unittest.TestCase):
    def test_the_shipped_profile_is_valid(self):
        profile = load(DEFAULT)
        self.assertEqual(validate(profile), [])

    def test_a_follow_wave_above_the_measured_cap_is_rejected(self):
        profile = _base()
        profile["follow"]["per_window"] = 40
        problems = validate(profile)
        self.assertTrue(any("follow.per_window" in p for p in problems))

    def test_the_exact_cap_is_allowed(self):
        profile = _base()
        profile["follow"]["per_window"] = 15
        self.assertEqual([p for p in validate(profile) if "per_window" in p], [])

    def test_a_navigation_timeout_over_the_ceiling_is_rejected(self):
        profile = _base()
        profile["runtime"]["nav_timeout_ms"] = 120000
        problems = validate(profile)
        self.assertTrue(any("nav_timeout_ms" in p for p in problems))

    def test_a_missing_required_key_is_reported(self):
        profile = _base()
        del profile["follow"]["window_seconds"]
        problems = validate(profile)
        self.assertTrue(any("follow.window_seconds" in p for p in problems))

    def test_a_non_boolean_enabled_flag_is_reported(self):
        profile = _base()
        profile["like"]["enabled"] = "no"
        problems = validate(profile)
        self.assertTrue(any("like.enabled" in p for p in problems))

    def test_empty_success_states_are_reported(self):
        profile = _base()
        profile["success_states"] = []
        problems = validate(profile)
        self.assertTrue(any("success_states" in p for p in problems))

    def test_a_missing_file_raises_a_typed_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ProfileError) as caught:
                load(Path(tmp) / "absent.json")
            self.assertEqual(caught.exception.code, "PROFILE_MISSING")

    def test_invalid_json_raises_a_typed_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ProfileError) as caught:
                load(path)
            self.assertEqual(caught.exception.code, "PROFILE_INVALID_JSON")

    def test_the_summary_derives_the_window_count_for_200_follows(self):
        result = summary(_base())
        self.assertEqual(result["follow_waves_for_200"], 15)
        self.assertEqual(result["follow_minutes_for_200"], 225.0)

    def test_the_shipped_profile_disables_likes(self):
        result = summary(load(DEFAULT))
        self.assertFalse(result["like_enabled"])


if __name__ == "__main__":
    unittest.main()
