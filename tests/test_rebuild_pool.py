"""A pool rebuilt from the ledger must drop finished work and keep the rest."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from rebuild_pool import rebuild  # noqa: E402


def _ledger(rows: list[dict]) -> dict:
    return {"actions": {("act_%d" % i): row for i, row in enumerate(rows)}}


def _write(directory: Path, ledger: dict) -> None:
    (directory / "state.json").write_text(
        json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
    )


def _repost(url: str, *, status: str, state: str | None = None) -> dict:
    return {
        "type": "repost_post",
        "status": status,
        "payload": {"post_url": url, "author": "a"},
        "result": {"state": state, "changed": True, "verified": True} if state else None,
    }


class RebuildPoolTest(unittest.TestCase):
    def test_finished_targets_are_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, _ledger([
                _repost("https://x.com/a/status/1", status="completed", state="reposted"),
                _repost("https://x.com/b/status/2", status="uncertain"),
                _repost("https://x.com/c/status/3", status="pending"),
            ]))
            result = rebuild(directory, action_type="repost_post")
            self.assertEqual(result["distinct_targets"], 3)
            self.assertEqual(result["finished"], 2)
            self.assertEqual(result["written"], 1)
            self.assertEqual(result["rows"][0]["url"], "https://x.com/c/status/3")

    def test_include_finished_keeps_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, _ledger([
                _repost("https://x.com/a/status/1", status="completed", state="reposted"),
                _repost("https://x.com/c/status/3", status="pending"),
            ]))
            result = rebuild(directory, action_type="repost_post", include_finished=True)
            self.assertEqual(result["written"], 2)

    def test_duplicate_targets_are_collapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, _ledger([
                _repost("https://x.com/a/status/1", status="pending"),
                _repost("https://x.com/a/status/1", status="pending"),
            ]))
            result = rebuild(directory, action_type="repost_post")
            self.assertEqual(result["distinct_targets"], 1)

    def test_a_follow_pool_uses_handle_and_profile_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, {"actions": {"act_1": {
                "type": "follow_profile",
                "status": "pending",
                "payload": {"handle": "someone", "profile_url": "https://x.com/someone"},
                "result": None,
            }}})
            result = rebuild(directory, action_type="follow_profile")
            self.assertEqual(result["written"], 1)
            row = result["rows"][0]
            self.assertEqual(row["handle"], "someone")
            self.assertEqual(row["profile_url"], "https://x.com/someone")

    def test_a_missing_state_file_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = rebuild(Path(tmp), action_type="repost_post")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "STATE_NOT_FOUND")

    def test_other_action_types_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write(directory, _ledger([
                {"type": "like_post", "status": "pending",
                 "payload": {"post_url": "https://x.com/z/status/9"}, "result": None},
            ]))
            result = rebuild(directory, action_type="repost_post")
            self.assertEqual(result["distinct_targets"], 0)


if __name__ == "__main__":
    unittest.main()
