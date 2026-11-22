"""A probe series must say whether the allowance moved or the followers did."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from follow_probe_report import load_rows, summarise  # noqa: E402


def _row(followers: str, *, code: int | None = None, state: str | None = None) -> dict:
    result = {"status": "failed", "state": state, "code": "X_WRITE_REJECTED"}
    if code is not None:
        result["rejection_code"] = code
    return {"at": "2026-11-14T13:00:00", "followers": followers, "result": result}


class FollowProbeReportTest(unittest.TestCase):
    def test_no_rows_reports_no_data(self):
        self.assertEqual(summarise([])["verdict"], "no_data")

    def test_refusals_with_flat_followers(self):
        report = summarise([_row("0", code=161), _row("0", code=161)])
        self.assertEqual(report["probes"], 2)
        self.assertEqual(report["outcomes"]["161"], 2)
        self.assertEqual(report["follower_changes"], 0)
        self.assertEqual(report["verdict"], "blocked_and_followers_flat")

    def test_refusals_while_followers_moved(self):
        report = summarise([_row("0", code=161), _row("3", code=161)])
        self.assertEqual(report["follower_changes"], 1)
        self.assertEqual(report["verdict"], "blocked_but_followers_moved")

    def test_one_success_means_the_allowance_is_open(self):
        report = summarise([_row("0", code=161), _row("5", state="following")])
        self.assertEqual(report["succeeded"], 1)
        self.assertEqual(report["verdict"], "allowance_open")

    def test_mixed_outcomes_are_reported_as_mixed(self):
        report = summarise([_row("0", state="following"), _row("1", code=161)])
        self.assertEqual(report["verdict"], "mixed")

    def test_bad_lines_are_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            path.write_text(
                '{"followers": "0", "result": {"rejection_code": 161}}\n'
                "not json\n"
                "\n"
                '{"followers": "0", "result": {"rejection_code": 161}}\n',
                encoding="utf-8",
            )
            rows = load_rows(path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(summarise(rows)["probes"], 2)

    def test_a_missing_log_yields_no_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_rows(Path(tmp) / "absent.jsonl"), [])

    def test_the_follower_series_is_preserved_in_order(self):
        report = summarise([_row("0"), _row("2"), _row("2"), _row("7")])
        self.assertEqual(report["follower_series"], ["0", "2", "2", "7"])
        self.assertEqual(report["follower_changes"], 2)


if __name__ == "__main__":
    unittest.main()
